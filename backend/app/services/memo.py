"""
backend/app/services/memo.py

Renewal / enhancement memo drafting (blueprint UC4). Assembles evidence-cited
memo sections from the deterministic analytics — NOT free LLM generation. Each
section carries explicit evidence_refs and a missing-data list, and the
Recommendation section is hard-gated: it can never say "approved".

The LLM layer (added later) may polish the prose of each section, but the facts,
evidence and recommendation STANCE come from here.
"""
from __future__ import annotations
from app.store import DataStore
from app.services.analytics import AccountConduct, EWSEngine, EnhancementAssessor


MEMO_SECTIONS = ["Executive summary", "Account conduct", "Risk observations",
                 "Mitigants", "Documents pending", "Recommendation"]


class MemoService:
    def __init__(self, store: DataStore, customer_id: str):
        self.s = store
        self.cid = customer_id
        self.cust = store.one("customer_master", customer_id=customer_id) or {}
        self.profile = store.one("business_profile", customer_id=customer_id) or {}
        self.conduct = AccountConduct(store, customer_id).summary()
        self.ews = EWSEngine(store, customer_id).signals()
        self.enh = EnhancementAssessor(store, customer_id).assess()
        self.docs = store.where("documents", customer_id=customer_id)

    def _pending_docs(self) -> list[str]:
        return sorted({d["document_type"] for d in self.docs
                       if d["status"] in ("Pending", "Expired") and d["required_flag"] == "Y"})

    def draft(self, memo_type: str = "renewal") -> dict:
        c = self.conduct
        name = self.cust.get("display_name", self.cid)
        critical = [s for s in self.ews if s["severity"] == "Critical"]
        high = [s for s in self.ews if s["severity"] == "High"]
        pending = self._pending_docs()

        sections = {}

        sections["Executive summary"] = {
            "text": (
                f"{name} is a {self.cust.get('constitution','')} MSME relationship "
                f"({self.profile.get('industry_description','')}). Credits are "
                f"{c['credits_trend_label']} ({c['credits_trend_pct']}% YoY trend), "
                f"average utilization {c['avg_utilization_pct']}%. "
                + ("Growth relationship; enhancement review recommended with conditions."
                   if self.enh["eligible_for_review"]
                   else "Caution relationship; renewal only after remediation.")
            ),
            "evidence_refs": ["analytics:summary", "customer_master", "business_profile"],
        }

        sections["Account conduct"] = {
            "text": (
                f"Average monthly credit INR {c['avg_monthly_credit_inr']:,.0f}, trend "
                f"{c['credits_trend_label']}. Peak utilization {c['peak_utilization_pct']}%, "
                f"{c['days_over_85_pct']} days above 85%. Cash intensity {c['cash_intensity_pct']}%. "
                f"Cheque returns: {c['cheque_return_count']}."
            ),
            "evidence_refs": ["transactions", "utilization", "cheque_returns"],
        }

        risk_text = "; ".join(f"{s['signal_type']} ({s['severity']}): {s['evidence_metric']}"
                              for s in self.ews) or "No material early-warning signals detected."
        sections["Risk observations"] = {
            "text": risk_text,
            "evidence_refs": ["ews_engine"],
            "guardrails": [s["false_positive_guardrail"] for s in self.ews],
        }

        mitigants = []
        if c["credits_trend_label"] == "rising":
            mitigants.append("rising GST/bank credits")
        if c["cheque_return_count"] <= 1:
            mitigants.append("clean repayment conduct")
        coll = self.s.where("collateral", customer_id=self.cid)
        if coll:
            mitigants.append("collateral on record")
        if not mitigants:
            mitigants.append("customer explanation of receivable delay to be verified; some seasonal recovery")
        sections["Mitigants"] = {"text": "; ".join(mitigants), "evidence_refs": ["collateral", "analytics:summary"]}

        sections["Documents pending"] = {
            "text": ("Pending/blocking: " + ", ".join(pending)) if pending else "No required documents pending.",
            "evidence_refs": ["documents"],
            "blocking": bool(critical),
        }

        # Recommendation — hard-gated, never an approval.
        if self.enh["eligible_for_review"]:
            rec = (f"Proceed with enhancement review (indicative band INR {self.enh['recommended_band_inr']}) "
                   f"subject to credit appraisal and the pending documents above. "
                   f"Caveats: {', '.join(self.enh['caveats'])}.")
        elif critical:
            rec = ("Do not enhance. Critical document/risk blockers must be resolved first. "
                   "Renew with caution only if bank policy conditions are satisfied after remediation.")
        else:
            rec = ("Renew with caution after document remediation and closer monitoring. "
                   "No enhancement recommended at this stage.")
        sections["Recommendation"] = {
            "text": rec,
            "evidence_refs": ["enhancement_assessor", "ews_engine"],
            "is_credit_decision": False,
            "disclaimer": "First draft for RM and credit review. Not a final credit document or approval.",
        }

        # audit + propose as a CRM write candidate (approval-gated)
        self.s.add_event("decision.memo_drafted", {
            "customer_id": self.cid, "memo_type": memo_type,
            "eligible": self.enh["eligible_for_review"], "critical_blockers": len(critical),
        })

        return {
            "customer_id": self.cid,
            "memo_type": memo_type,
            "sections": [{"section": k, **v} for k, v in sections.items()],
            "missing_data": pending,
            "recommendation_eligible": self.enh["eligible_for_review"],
            "disclaimer": "Draft only. Human-in-the-loop: RM and credit review required.",
        }
