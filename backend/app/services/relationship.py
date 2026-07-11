"""
backend/app/services/relationship.py

Composite customer dossier for the POC dashboard. This service deliberately
surfaces the synthetic data pack at CRM level, so the RM cockpit can demonstrate
that every narrative/nudge is grounded in actual account, credit, operations,
CRM, GST and transaction rows rather than in a generic assistant response.
"""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime
from app.store import DataStore
from app.services.analytics import AccountConduct, EWSEngine, EnhancementAssessor
from app.services.crosssell import opportunities, derive_signals


def _num(v, default: float = 0.0) -> float:
    try:
        if v in (None, ""):
            return default
        return float(v)
    except Exception:
        return default


def _latest(rows: list[dict], date_key: str, n: int = 5) -> list[dict]:
    return sorted(rows, key=lambda r: r.get(date_key, ""), reverse=True)[:n]


def _month_tail(series: dict[str, float], n: int = 6) -> list[dict]:
    return [{"period": k, "amount_inr": v} for k, v in list(series.items())[-n:]]


def _top_counterparties(store: DataStore, customer_id: str, n: int = 5) -> list[dict]:
    totals: dict[str, dict] = {}
    for t in store.where("transactions", customer_id=customer_id):
        cp = t.get("counterparty_name") or "Unknown"
        if cp not in totals:
            totals[cp] = {
                "counterparty_name": cp,
                "counterparty_type": t.get("counterparty_type"),
                "credits_inr": 0.0,
                "debits_inr": 0.0,
                "transaction_count": 0,
            }
        totals[cp]["transaction_count"] += 1
        if t.get("dr_cr") == "CR":
            totals[cp]["credits_inr"] += _num(t.get("amount_inr"))
        else:
            totals[cp]["debits_inr"] += _num(t.get("amount_inr"))
    rows = list(totals.values())
    for r in rows:
        r["total_value_inr"] = round(r["credits_inr"] + r["debits_inr"], 2)
        r["credits_inr"] = round(r["credits_inr"], 2)
        r["debits_inr"] = round(r["debits_inr"], 2)
    return sorted(rows, key=lambda r: r["total_value_inr"], reverse=True)[:n]


def recent_transactions(store: DataStore, customer_id: str, limit: int = 12) -> list[dict]:
    txns = _latest(store.where("transactions", customer_id=customer_id), "txn_timestamp", limit)
    return [{
        "txn_id": t.get("txn_id"),
        "txn_date": t.get("txn_date"),
        "dr_cr": t.get("dr_cr"),
        "amount_inr": _num(t.get("amount_inr")),
        "counterparty_name": t.get("counterparty_name"),
        "category_lvl1": t.get("category_lvl1"),
        "category_lvl2": t.get("category_lvl2"),
        "channel": t.get("channel"),
        "is_return": t.get("is_return"),
        "return_reason": t.get("return_reason"),
        "anomaly_tag": t.get("anomaly_tag"),
        "description": t.get("description"),
    } for t in txns]



def query_transactions(store: DataStore, customer_id: str, *, operation: str = "recent",
                       direction: str = "all", limit: int = 5, period_days: int | None = None,
                       merchant: str | None = None, category: str | None = None) -> dict:
    """Deterministic transaction tool used by the live transcript AI planner.

    The model chooses the operation and parameters; this function performs the
    actual record lookup and arithmetic so amounts and rankings cannot be invented.
    """
    from datetime import date, datetime, timedelta
    rows = []
    for t in store.where("transactions", customer_id=customer_id):
        r = {
            "txn_id": t.get("txn_id"), "txn_timestamp": t.get("txn_timestamp"),
            "txn_date": t.get("txn_date"), "dr_cr": t.get("dr_cr"),
            "amount_inr": _num(t.get("amount_inr")),
            "counterparty_name": t.get("counterparty_name"),
            "category_lvl1": t.get("category_lvl1"), "category_lvl2": t.get("category_lvl2"),
            "channel": t.get("channel"), "is_return": t.get("is_return"),
            "return_reason": t.get("return_reason"), "anomaly_tag": t.get("anomaly_tag"),
            "description": t.get("description"),
        }
        rows.append(r)
    rows.sort(key=lambda r: r.get("txn_timestamp") or r.get("txn_date") or "", reverse=True)
    total_unfiltered = len(rows)
    d = (direction or "all").lower()
    if d in {"debit", "dr", "spend", "spends"}: rows = [r for r in rows if str(r.get("dr_cr")).upper() == "DR"]
    elif d in {"credit", "cr", "inflow", "credits"}: rows = [r for r in rows if str(r.get("dr_cr")).upper() == "CR"]
    if merchant:
        q = merchant.lower().strip()
        rows = [r for r in rows if q in ((r.get("counterparty_name") or "") + " " + (r.get("description") or "")).lower()]
    if category:
        q = category.lower().strip()
        rows = [r for r in rows if q in ((r.get("category_lvl1") or "") + " " + (r.get("category_lvl2") or "")).lower()]
    if period_days:
        dated = []
        parsed = []
        for r in rows:
            raw = r.get("txn_date") or str(r.get("txn_timestamp") or "")[:10]
            try: parsed.append((date.fromisoformat(raw), r))
            except Exception: pass
        if parsed:
            anchor = max(x[0] for x in parsed)
            cutoff = anchor - timedelta(days=max(1, int(period_days)))
            rows = [r for dt, r in parsed if dt >= cutoff]
            dated = rows
    op = (operation or "recent").lower()
    n = max(1, min(int(limit or 5), 20))
    selected = list(rows)
    if op in {"largest", "highest", "top"}:
        selected = sorted(rows, key=lambda r: r.get("amount_inr") or 0, reverse=True)[:n]
    elif op in {"smallest", "lowest"}:
        selected = sorted(rows, key=lambda r: r.get("amount_inr") or 0)[:n]
    elif op in {"recent", "latest", "list", "search"}:
        selected = rows[:n]
    elif op in {"aggregate", "summary", "total"}:
        selected = rows[:n]
    else:
        selected = rows[:n]
    total_amount = round(sum(_num(r.get("amount_inr")) for r in rows), 2)
    avg_amount = round(total_amount / len(rows), 2) if rows else 0.0
    debits = [r for r in rows if str(r.get("dr_cr")).upper() == "DR"]
    credits = [r for r in rows if str(r.get("dr_cr")).upper() == "CR"]
    grouped: dict[str, dict] = {}
    for r in rows:
        key = r.get("counterparty_name") or r.get("description") or "Unknown"
        g = grouped.setdefault(key, {"name": key, "amount_inr": 0.0, "count": 0})
        g["amount_inr"] += _num(r.get("amount_inr")); g["count"] += 1
    top_counterparties = sorted(grouped.values(), key=lambda x: x["amount_inr"], reverse=True)[:10]
    for g in top_counterparties: g["amount_inr"] = round(g["amount_inr"], 2)
    categories: dict[str, dict] = {}
    for r in rows:
        key = r.get("category_lvl2") or r.get("category_lvl1") or "Uncategorised"
        g = categories.setdefault(key, {"name": key, "amount_inr": 0.0, "count": 0})
        g["amount_inr"] += _num(r.get("amount_inr")); g["count"] += 1
    top_categories = sorted(categories.values(), key=lambda x: x["amount_inr"], reverse=True)[:10]
    for g in top_categories: g["amount_inr"] = round(g["amount_inr"], 2)
    return {
        "customer_id": customer_id, "operation": op, "direction": d,
        "filters": {"period_days": period_days, "merchant": merchant, "category": category},
        "rows_scanned": total_unfiltered, "rows_matched": len(rows),
        "transactions": selected,
        "metrics": {
            "total_amount_inr": total_amount, "average_amount_inr": avg_amount,
            "debit_count": len(debits), "debit_total_inr": round(sum(r["amount_inr"] for r in debits), 2),
            "credit_count": len(credits), "credit_total_inr": round(sum(r["amount_inr"] for r in credits), 2),
        },
        "top_counterparties": top_counterparties, "top_categories": top_categories,
    }


def transaction_insights(store: DataStore, customer_id: str) -> dict:
    """Pre-warmed common transaction views for low-latency in-call answers."""
    return {
        "recent": query_transactions(store, customer_id, operation="recent", limit=20),
        "largest_all": query_transactions(store, customer_id, operation="largest", direction="all", limit=20),
        "largest_debits": query_transactions(store, customer_id, operation="largest", direction="debit", limit=20),
        "largest_credits": query_transactions(store, customer_id, operation="largest", direction="credit", limit=20),
        "last_30_days": query_transactions(store, customer_id, operation="aggregate", direction="all", period_days=30, limit=10),
    }

def crm_timeline(store: DataStore, customer_id: str) -> dict:
    interactions = _latest(store.where("interactions", customer_id=customer_id), "interaction_date", 8)
    service_requests = _latest(store.where("service_requests", customer_id=customer_id), "created_date", 8)
    tasks = _latest(store.where("tasks", customer_id=customer_id), "due_date", 8)
    opps = store.where("opportunities", customer_id=customer_id)
    committed = [c for c in store.committed if c.get("customer_id") == customer_id]
    pending = [c for c in store.pending_writes if c.get("customer_id") == customer_id and c.get("status") == "pending_approval"]

    events: list[dict] = []
    for i in interactions:
        rc = i.get("rich_case") or {}
        events.append({
            "type": "interaction", "date": i.get("interaction_date"), "title": i.get("subject"),
            "status": i.get("sentiment"), "detail": rc.get("narrative") or i.get("summary"),
            "rich": rc or None,
            "evidence": [i.get("interaction_id"), i.get("channel")],
        })
    for s in service_requests:
        rc = s.get("rich_case") or {}
        events.append({
            "type": "service", "date": s.get("created_date"), "title": f"{s.get('category')} · {s.get('ticket_id')}",
            "status": s.get("status"), "detail": rc.get("narrative") or s.get("description"),
            "rich": rc or None,
            "evidence": [f"priority {s.get('priority')}", f"SLA {s.get('sla_due_date')}", s.get("remarks")],
        })
    for t in tasks:
        rc = t.get("rich_case") or {}
        events.append({
            "type": "task", "date": t.get("due_date"), "title": t.get("title"),
            "status": t.get("status"), "detail": rc.get("narrative") or f"Priority {t.get('priority')} · approval {t.get('approval_state')}",
            "rich": rc or None,
            "evidence": [t.get("task_id"), t.get("created_by")],
        })
    for o in opps:
        rc = o.get("rich_case") or {}
        events.append({
            "type": "opportunity", "date": "2026-05-24", "title": o.get("opportunity_type"),
            "status": o.get("status"), "detail": rc.get("narrative") or f"Stage {o.get('stage')}; band {o.get('recommended_band_inr') or 'n/a'}; blockers {o.get('blockers') or 'none'}",
            "rich": rc or None,
            "evidence": [o.get("opportunity_id")],
        })
    for c in committed:
        payload = c.get("payload", {})
        title = payload.get("title") or payload.get("summary") or payload.get("action") or c.get("type")
        events.append({
            "type": "ai_writeback", "date": c.get("approved_at", c.get("created_at")), "title": title,
            "status": "Saved", "detail": str(payload),
            "evidence": c.get("evidence_refs", []),
        })
    for c in pending:
        events.append({
            "type": "pending_ai_write", "date": c.get("created_at"), "title": c.get("type"),
            "status": "Pending RM approval", "detail": str(c.get("payload", {})),
            "evidence": c.get("evidence_refs", []),
        })
    events.sort(key=lambda e: e.get("date") or "", reverse=True)
    return {"customer_id": customer_id, "events": events[:30], "pending_count": len(pending), "committed_count": len(committed)}


def relationship_dossier(store: DataStore, customer_id: str) -> dict:
    cust = store.one("customer_master", customer_id=customer_id) or {}
    prof = store.one("business_profile", customer_id=customer_id) or {}
    facility = store.one("facilities", customer_id=customer_id) or {}
    conduct = AccountConduct(store, customer_id).summary()
    ews = EWSEngine(store, customer_id).signals()
    enh = EnhancementAssessor(store, customer_id).assess()
    opps = opportunities(store, customer_id)

    gst = _latest(store.where("gst", customer_id=customer_id), "period", 12)
    aging = store.one("aging", customer_id=customer_id) or {}
    bureau = store.one("bureau", customer_id=customer_id) or {}
    insurance = store.where("insurance", customer_id=customer_id)
    collateral = store.where("collateral", customer_id=customer_id)
    stock = _latest(store.where("stock_statements", customer_id=customer_id), "period", 6)
    repayments = _latest(store.where("repayments", customer_id=customer_id), "due_date", 6)
    documents = store.where("documents", customer_id=customer_id)
    tasks = [t for t in store.where("tasks", customer_id=customer_id) if t.get("status") == "Open"]
    srs = store.where("service_requests", customer_id=customer_id)

    totals = {
        "total_credits_inr": round(sum(conduct.get("monthly_credits", {}).values()), 2),
        "total_debits_inr": round(sum(conduct.get("monthly_debits", {}).values()), 2),
        "open_service_tickets": len([s for s in srs if s.get("status") == "Open"]),
        "open_tasks": len(tasks),
        "pending_documents": len([d for d in documents if d.get("status") in ("Pending", "Expired", "Overdue")]),
        "eligible_opportunities": len([o for o in opps if o.get("eligible")]),
        "blocked_opportunities": len([o for o in opps if not o.get("eligible")]),
    }

    return {
        "customer_id": customer_id,
        "customer": cust,
        "business_profile": prof,
        "facility": facility,
        "summary_metrics": totals,
        "signals": sorted(list(derive_signals(store, customer_id))),
        "conduct": conduct,
        "ews": ews,
        "enhancement": enh,
        "opportunities": opps,
        "engagement_threads": store.where("engagement_threads", customer_id=customer_id),
        "stakeholders": store.where("stakeholders", customer_id=customer_id),
        "gst_monthly": list(reversed(gst)),
        "aging": aging,
        "bureau": bureau,
        "insurance": insurance,
        "collateral": collateral,
        "stock_statements": list(reversed(stock)),
        "repayments": list(reversed(repayments)),
        "documents": documents,
        "top_counterparties": _top_counterparties(store, customer_id, 6),
        "recent_transactions": recent_transactions(store, customer_id, 10),
        "crm_timeline": crm_timeline(store, customer_id)["events"],
        "month_tail": {
            "credits": _month_tail(conduct.get("monthly_credits", {}), 6),
            "debits": _month_tail(conduct.get("monthly_debits", {}), 6),
        },
        "dataset_footprint": {
            "transactions": len(store.where("transactions", customer_id=customer_id)),
            "daily_balances": len(store.where("daily_balances", customer_id=customer_id)),
            "daily_utilization": len(store.where("utilization", customer_id=customer_id)),
            "gst_returns": len(store.where("gst", customer_id=customer_id)),
            "repayments": len(store.where("repayments", customer_id=customer_id)),
            "crm_interactions": len(store.where("interactions", customer_id=customer_id)),
            "service_requests": len(srs),
            "documents": len(documents),
            "counterparties": len(store.where("counterparties", customer_id=customer_id)),
        },
    }


def live_call_playbook(store: DataStore, customer_id: str) -> dict:
    cust = store.one("customer_master", customer_id=customer_id) or {}
    prof = store.one("business_profile", customer_id=customer_id) or {}
    conduct = AccountConduct(store, customer_id).summary()
    ews = EWSEngine(store, customer_id).signals()
    enh = EnhancementAssessor(store, customer_id).assess()
    opps = opportunities(store, customer_id)
    eligible = [o for o in opps if o.get("eligible")]
    blocked = [o for o in opps if not o.get("eligible")]
    docs = [d for d in store.where("documents", customer_id=customer_id) if d.get("status") in ("Pending", "Expired", "Overdue")]
    open_sr = [s for s in store.where("service_requests", customer_id=customer_id) if s.get("status") == "Open"]

    if enh.get("eligible_for_review"):
        objective = "Validate growth and initiate a limit-enhancement review without committing approval."
        opening = f"I see your credits and utilization have moved up with the {prof.get('industry_description','business')} cycle. Let us validate the order pipeline and documents before we put a review note to credit."
    elif any(s.get("severity") == "Critical" for s in ews):
        objective = "Service recovery and risk remediation first; avoid new lending commitment."
        opening = "Before discussing any enhancement, I want to close the document and conduct items so your renewal is not delayed."
    else:
        objective = "Prepare renewal discussion and identify safe, relevant transaction-banking opportunities."
        opening = "Let us review renewal readiness, recent conduct and any operational support you need from the bank."

    return {
        "customer_id": customer_id,
        "display_name": cust.get("display_name"),
        "primary_objective": objective,
        "opening_script": opening,
        "talk_tracks": [
            {"label": "Start with", "text": "Acknowledge last CRM interaction and any open service ticket before selling."},
            {"label": "Validate", "text": "Ask for PO/order copy, latest GST, debtor aging and stock statement where relevant."},
            {"label": "Position", "text": (eligible[0]["product"] + " — " + eligible[0]["rationale"]) if eligible else "No eligible cross-sell today; stabilize the relationship first."},
            {"label": "Close", "text": "Summarize next action, owner and due date; save as CRM note/task."},
        ],
        "do_say": [
            "I can initiate a review subject to credit appraisal and documents.",
            "Let me verify the data points and revert with a structured checklist.",
            "This is a relationship review, not a sanction commitment on the call.",
        ],
        "dont_say": [
            "Your enhancement is approved.",
            "The bank has already sanctioned the higher limit.",
            "This conduct definitely indicates default or fund diversion.",
        ],
        "live_nudge_triggers": [
            "OEM order / purchase order / new contract",
            "increase limit / need working capital / more funds",
            "cheque bounce / buyer delay / receivable stuck",
            "charges complaint / service dissatisfaction / another bank",
            "already submitted document / insurance copy / stock statement",
            "payroll, supplier payments, cash collections, LC/BG, invoice discounting",
        ],
        "data_to_keep_open": [
            f"Credits trend {conduct['credits_trend_label']} {conduct['credits_trend_pct']}%",
            f"Avg utilization {conduct['avg_utilization_pct']}%, peak {conduct['peak_utilization_pct']}%",
            f"Top buyer concentration {conduct['top_counterparty_concentration_pct']}%",
            f"Pending docs: {', '.join(sorted({d['document_type'] for d in docs})) or 'none'}",
            f"Open service tickets: {len(open_sr)}",
            f"Eligible opportunities: {', '.join(o['product'] for o in eligible[:3]) or 'none'}",
            f"Blocked opportunities: {', '.join(o['product'] for o in blocked[:2]) or 'none'}",
        ],
    }
