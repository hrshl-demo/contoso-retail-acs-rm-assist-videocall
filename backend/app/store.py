"""
backend/app/store.py

DataStore — loads the synthetic CSV pack into memory at container startup.

This is the SWAP SEAM (same philosophy as the loans POC's RiskEngine.assess()):
every consumer talks to the `DataStore` interface, never to pandas/CSV directly.
To move to Azure SQL / PostgreSQL later, implement the same methods against the
DB and swap the construction in main.py — no caller changes.

For the POC the data is read-only and fits comfortably in RAM (~3.2k transactions,
~730 daily rows), so we use pandas DataFrames for the analytical tables and plain
dicts for the small master tables. Mutable runtime state (proposed CRM writes,
audit events, approvals) lives here too and resets on restart.
"""
from __future__ import annotations
import csv
import os
from pathlib import Path
from typing import Any
from datetime import datetime


class DataStore:
    def __init__(self):
        # static (seed) tables
        self.tables: dict[str, list[dict]] = {}
        # mutable runtime state (reset on restart)
        self.pending_writes: list[dict] = []     # CRM update candidates awaiting RM approval
        self.committed: list[dict] = []          # approved writes
        self.events: list[dict] = []             # append-only audit log (glass-box stream)
        self.call_records: list[dict] = []        # downloadable post-call transcripts / AI event packs
        self.hni_cases_by_id: dict[str, dict] = {} # seeded HNI service-recovery cases
        self.hni_cases_by_customer: dict[str, dict] = {}
        self.hni_runtime: dict[str, dict] = {}      # mutable workflow/email state (POC memory)
        self._loaded = False

    # ---------- loading ----------
    def load(self, data_dir: Path, kb_dir: Path | None = None):
        data_dir = Path(data_dir)
        mapping = {
            "customer_master": "01_master_data/customer_master.csv",
            "business_profile": "01_master_data/msme_business_profile.csv",
            "promoters": "01_master_data/promoters_guarantors.csv",
            "stakeholders": "01_master_data/stakeholders.csv",
            "portfolio": "01_master_data/portfolio_assignments.csv",
            "accounts": "02_accounts/accounts.csv",
            "transactions": "02_accounts/current_account_transactions_fy2025_26.csv",
            "daily_balances": "02_accounts/daily_balances.csv",
            "counterparties": "02_accounts/counterparty_master.csv",
            "facilities": "03_credit/loan_facilities.csv",
            "utilization": "03_credit/daily_limit_utilization.csv",
            "repayments": "03_credit/repayment_history.csv",
            "collateral": "03_credit/collateral_security.csv",
            "covenants": "03_credit/facility_covenants.csv",
            "stock_statements": "03_credit/stock_statements.csv",
            "insurance": "03_credit/insurance_status.csv",
            "gst": "04_financials/gst_returns_monthly.csv",
            "financials": "04_financials/financial_statements_summary.csv",
            "aging": "04_financials/debtor_creditor_aging.csv",
            "bureau": "04_financials/bureau_summary.csv",
            "documents": "05_operations/document_status.csv",
            "service_requests": "05_operations/service_requests.csv",
            "cheque_returns": "05_operations/cheque_returns.csv",
            "consent": "05_operations/consent_registry.csv",
            "interactions": "06_crm/rm_interactions.csv",
            "engagement_threads": "06_crm/engagement_threads.csv",
            "tasks": "06_crm/crm_tasks.csv",
            "opportunities": "06_crm/opportunities.csv",
            "audit_seed": "06_crm/audit_log.csv",
            "rm_activity": "08_rm/rm_daily_activity.csv",
        }
        for key, rel in mapping.items():
            self.tables[key] = self._read_csv(data_dir / rel)
        if kb_dir:
            kb_dir = Path(kb_dir)
            for key, fn in {"product_rules": "product_rules.csv",
                            "checklist_rules": "document_checklist_rules.csv",
                            "product_catalog": "product_catalog.csv",
                            "marketing_templates": "marketing_templates.csv",
                            "policy_manifest": "policy_documents_manifest.csv",
                            "solution_playbooks": "solution_playbooks.csv"}.items():
                p = kb_dir / fn
                if p.exists():
                    self.tables[key] = self._read_csv(p)
        # Overlay AI-enriched CRM case records (one-time gen, committed) keyed by
        # case_id across interactions/opportunities/tasks/service_requests. Survives
        # deterministic rebuilds (separate committed file, not the regenerated CSV).
        if kb_dir:
            enriched_path = Path(kb_dir) / "crm_cases_enriched.csv"
            if enriched_path.exists():
                import json as _json
                rich_by_id = {}
                for r in self._read_csv(enriched_path):
                    try:
                        rich_by_id[r["case_id"]] = _json.loads(r.get("rich_json") or "{}")
                    except Exception:
                        rich_by_id[r["case_id"]] = {}
                self.enriched_cases = rich_by_id
                id_fields = {"interactions": "interaction_id", "opportunities": "opportunity_id",
                             "tasks": "tasks_id", "service_requests": "ticket_id"}
                # tasks table id field is task_id; map robustly
                for table, rows in self.tables.items():
                    for row in rows:
                        for idf in ("interaction_id", "opportunity_id", "task_id", "ticket_id"):
                            cid_val = row.get(idf)
                            if cid_val and cid_val in rich_by_id:
                                row["rich_case"] = rich_by_id[cid_val]
                                break

        # Lean Rakesh build: the Kavita (rescue) and Vikram (HNI) journeys are not
        # shipped. These attributes remain as empty maps so shared services that
        # probe them via getattr(...) safely no-op for retail RM-assist customers.
        self.rescue = None
        self.rescue_cases_by_id = {}
        self.rescue_cases_by_customer = {}

        self._loaded = True
        return self

    @staticmethod
    def _read_csv(path: Path) -> list[dict]:
        if not path.exists():
            return []
        with open(path, newline="") as f:
            return list(csv.DictReader(f))

    # ---------- generic accessors (the swap seam) ----------
    def all(self, table: str) -> list[dict]:
        return self.tables.get(table, [])

    def where(self, table: str, **filters) -> list[dict]:
        rows = self.tables.get(table, [])
        out = []
        for r in rows:
            if all(str(r.get(k, "")) == str(v) for k, v in filters.items()):
                out.append(r)
        return out

    def one(self, table: str, **filters) -> dict | None:
        rows = self.where(table, **filters)
        return rows[0] if rows else None

    def customers(self) -> list[dict]:
        return self.all("customer_master")

    # ---------- runtime state ----------
    def add_event(self, event_type: str, payload: dict) -> dict:
        ev = {
            "event_id": f"EV-{len(self.events)+1:05d}",
            "event_type": event_type,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "payload": payload,
        }
        self.events.append(ev)
        return ev

    # ---------- call records / transcript artifacts ----------
    def save_call_record(self, record: dict) -> dict:
        """Persist a post-call record in the POC runtime store.

        This is the production swap seam for Blob Storage / Dataverse / a Work IQ
        ingestion store. The record contains the role-tagged transcript, AI events,
        CRM cases created during the call, and the final wrap-up.
        """
        rec = dict(record or {})
        record_id = str(rec.get("record_id") or f"CALLREC-{len(self.call_records)+1:05d}")
        existing = next((r for r in self.call_records if r.get("record_id") == record_id), None)
        rec["record_id"] = record_id
        rec.setdefault("created_at", datetime.utcnow().isoformat() + "Z")
        rec.setdefault("status", "final")
        if existing is not None:
            existing.clear(); existing.update(rec)
            saved = existing
        else:
            self.call_records.append(rec)
            saved = rec
        self.tables["call_records"] = self.call_records
        self.add_event("call_record.saved", {
            "record_id": record_id,
            "session_id": saved.get("session_id"),
            "customer_id": saved.get("customer_id"),
            "transcript_turns": len(saved.get("transcript") or []),
        })
        return saved

    def call_records_for_customer(self, customer_id: str) -> list[dict]:
        rows = [r for r in self.call_records if str(r.get("customer_id")) == str(customer_id)]
        return sorted(rows, key=lambda r: r.get("ended_at") or r.get("created_at") or "", reverse=True)

    def get_call_record(self, record_id: str) -> dict | None:
        return next((r for r in self.call_records if str(r.get("record_id")) == str(record_id)), None)

    def propose_write(self, candidate: dict) -> dict:
        cand = dict(candidate)
        cand["candidate_id"] = f"CAND-{len(self.pending_writes)+1:05d}"
        cand["status"] = "pending_approval"
        cand["created_at"] = datetime.utcnow().isoformat() + "Z"
        self.pending_writes.append(cand)
        self.add_event("crm.update_candidate", {"candidate_id": cand["candidate_id"],
                                                "type": cand.get("type"), "customer_id": cand.get("customer_id")})
        return cand

    def approve_write(self, candidate_id: str, approver: str, edited: dict | None = None) -> dict | None:
        for c in self.pending_writes:
            if c["candidate_id"] == candidate_id and c["status"] == "pending_approval":
                c["status"] = "approved"
                c["approver"] = approver
                c["approved_at"] = datetime.utcnow().isoformat() + "Z"
                if edited:
                    c["payload"] = {**c.get("payload", {}), **edited}
                self.committed.append(c)
                self._materialize_crm_write(c)
                self.add_event("crm.saved", {"candidate_id": candidate_id, "approver": approver,
                                             "customer_id": c.get("customer_id"), "type": c.get("type")})
                return c
        return None

    def _materialize_crm_write(self, c: dict) -> None:
        """Reflect approved AI/RM updates into the in-memory CRM tables so the
        dashboard changes immediately during the demo. This is deliberately
        simple and reset-on-restart; a production build would persist the same
        shape to Dynamics/Salesforce/SQL via this seam."""
        cid = c.get("customer_id", "")
        payload = c.get("payload", {}) or {}
        typ = (c.get("type") or "note").lower()
        now = datetime.utcnow()
        date_s = now.date().isoformat()
        if typ in ("note", "interaction"):
            self.tables.setdefault("interactions", []).append({
                "interaction_id": f"INT-AI-{len(self.tables.get('interactions', []))+1:05d}",
                "customer_id": cid, "rm_id": payload.get("rm_id", "RM-1042"),
                "interaction_date": date_s, "channel": payload.get("channel", "Live call"),
                "subject": payload.get("subject", payload.get("intent", "AI-assisted RM note")),
                "summary": payload.get("summary", payload.get("action", str(payload))),
                "commitments_by_customer": payload.get("commitments_by_customer", "To be confirmed"),
                "commitments_by_bank": payload.get("commitments_by_bank", "RM follow-up"),
                "next_follow_up_date": payload.get("next_follow_up_date", ""),
                "sentiment": payload.get("sentiment", "Neutral"),
                "linked_task_id": payload.get("linked_task_id", ""),
                "created_by": "AI+RM",
            })
        elif typ == "task":
            self.tables.setdefault("tasks", []).append({
                "task_id": f"TASK-AI-{len(self.tables.get('tasks', []))+1:05d}",
                "customer_id": cid, "rm_id": payload.get("rm_id", "RM-1042"),
                "title": payload.get("title", payload.get("action", "AI-assisted follow-up")),
                "due_date": payload.get("due", payload.get("due_date", date_s)),
                "status": payload.get("status", "Open"),
                "priority": payload.get("priority", "Medium"),
                "created_by": "AI+RM", "approval_state": "Approved",
            })
        elif typ == "opportunity":
            self.tables.setdefault("opportunities", []).append({
                "opportunity_id": f"OPP-AI-{len(self.tables.get('opportunities', []))+1:05d}",
                "customer_id": cid,
                "opportunity_type": payload.get("opportunity_type", payload.get("product", "AI identified opportunity")),
                "stage": payload.get("stage", "Identified"),
                "recommended_band_inr": payload.get("recommended_band_inr", ""),
                "status": payload.get("status", "Open"),
                "blockers": payload.get("blockers", ""),
            })
