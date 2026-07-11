"""
backend/app/services/portfolio.py

Portfolio priority queue (UC1) and Customer 360 (UC2). Deterministic ranking and
aggregation over the store + analytics engines.
"""
from __future__ import annotations
from app.store import DataStore
from app.services.analytics import AccountConduct, EWSEngine, EnhancementAssessor


def priority_queue(store: DataStore, rm_id: str | None = None) -> list[dict]:
    """Rank customers into Growth / Renewal Due / Risk Watch / Docs Pending buckets
    with a reason and recommended action (UC1)."""
    out = []
    for cust in store.customers():
        cid = cust["customer_id"]
        if rm_id and cust.get("rm_id") != rm_id:
            continue
        # HNI multi-party service-recovery cases take top priority before the
        # generic relationship journey. They are not financial-crime rescue cases.
        hni_case = (getattr(store, "hni_cases_by_customer", {}) or {}).get(cid)
        if hni_case:
            payment = hni_case.get("payment", {})
            out.append({
                "customer_id": cid, "display_name": cust.get("display_name"),
                "bucket": "Customer Intervention", "priority": 0,
                "reason": f"Urgent {payment.get('amountInr', 0)/100000:.0f} lakh overseas medical payment held for verification",
                "recommended_action": "Open HNI multi-party resolution",
                "case_kind": "HNI_URGENT_PAYMENT_SERVICE_RECOVERY",
                "case_id": hni_case.get("caseId"),
                "high_signals": 1, "critical_signals": 1,
                "blocking_documents": len([d for d in hni_case.get("documents", []) if d.get("status") == "Pending"]),
                "relationship_value_score": int(cust.get("relationship_value_score") or 0),
                "hni_case": True,
            })
            continue
        # AI investigation cases take top priority, with case-specific framing.
        rescue_case = (getattr(store, "rescue_cases_by_customer", {}) or {}).get(cid)
        if rescue_case or str(cust.get("restriction_flag", "")).upper() == "Y":
            docs0 = store.where("documents", customer_id=cid)
            ui = (rescue_case or {}).get("ui", {})
            summary = (rescue_case or {}).get("caseSummary", {})
            out.append({
                "customer_id": cid, "display_name": cust.get("display_name"),
                "bucket": "Customer Intervention", "priority": 0,
                "reason": ui.get("queueReason", "Customer intervention case requires RM attention"),
                "recommended_action": ui.get("queueAction", "Open AI investigation"),
                "case_kind": (rescue_case or {}).get("caseKind", summary.get("caseType")),
                "case_id": summary.get("caseId"),
                "high_signals": 1, "critical_signals": 1,
                "blocking_documents": len([d for d in docs0 if d.get("blocking_flag") == "Y"]),
                "relationship_value_score": int(cust.get("relationship_value_score") or 0),
                "rescue_case": True,
            })
            continue
        ews = EWSEngine(store, cid).signals()
        enh = EnhancementAssessor(store, cid).assess()
        critical = [s for s in ews if s["severity"] == "Critical"]
        high = [s for s in ews if s["severity"] == "High"]

        if critical or len(high) >= 2:
            bucket, reason, action = ("Risk Watch",
                f"{len(high)} high + {len(critical)} critical signal(s)",
                "Risk-first call; watchlist; document remediation")
            priority = 1
        elif enh["eligible_for_review"]:
            bucket, reason, action = ("Growth",
                "Enhancement candidate: rising credits, clean conduct",
                "Prepare enhancement talking points; collect documents")
            priority = 2
        else:
            bucket, reason, action = ("Renewal Due",
                "Renewal review pending", "Prepare renewal brief")
            priority = 3

        # docs-pending overlay
        docs = store.where("documents", customer_id=cid)
        blocking = [d for d in docs if d.get("blocking_flag") == "Y"]
        out.append({
            "customer_id": cid,
            "display_name": cust.get("display_name"),
            "bucket": bucket,
            "priority": priority,
            "reason": reason,
            "recommended_action": action,
            "high_signals": len(high),
            "critical_signals": len(critical),
            "blocking_documents": len(blocking),
            "relationship_value_score": int(cust.get("relationship_value_score") or 0),
        })
    return sorted(out, key=lambda x: (x["priority"], -x["relationship_value_score"]))


def customer_360(store: DataStore, customer_id: str) -> dict:
    cust = store.one("customer_master", customer_id=customer_id)
    if not cust:
        return {}
    profile = store.one("business_profile", customer_id=customer_id) or {}
    facility = store.one("facilities", customer_id=customer_id) or {}
    promoters = store.where("promoters", customer_id=customer_id)
    conduct = AccountConduct(store, customer_id).summary()
    ews = EWSEngine(store, customer_id).signals()
    enh = EnhancementAssessor(store, customer_id).assess()
    docs = store.where("documents", customer_id=customer_id)
    pending_docs = [d for d in docs if d["status"] in ("Pending", "Expired") and d["required_flag"] == "Y"]
    open_sr = [r for r in store.where("service_requests", customer_id=customer_id) if r["status"] == "Open"]

    return {
        "customer": cust,
        "business_profile": profile,
        "primary_facility": facility,
        "promoters": promoters,
        "conduct_summary": conduct,
        "ews_signals": ews,
        "enhancement": enh,
        "documents_pending": pending_docs,
        "open_service_requests": open_sr,
        "call_records": [{
            "record_id": r.get("record_id"),
            "session_id": r.get("session_id"),
            "mode": r.get("mode"),
            "started_at": r.get("started_at"),
            "ended_at": r.get("ended_at"),
            "capture_scope": r.get("capture_scope"),
            "transcript_turns": len(r.get("transcript") or []),
            "headline": (r.get("summary") or {}).get("subject") or "Call transcript",
        } for r in store.call_records_for_customer(customer_id)],
        "next_best_questions": _next_best_questions(enh, ews),
    }


def _next_best_questions(enh: dict, ews: list[dict]) -> list[str]:
    qs = []
    if enh["eligible_for_review"]:
        qs += [
            "Confirm the new order pipeline and expected turnover.",
            "Request latest GST returns, stock statement, debtor aging and PO copy.",
            "Discuss buyer concentration and any diversification plans.",
        ]
    if any(s["signal_type"] == "Declining credits" for s in ews):
        qs.append("Which buyers have delayed payment, and what amounts/dates are expected?")
    if any(s["signal_type"] == "Cheque return" for s in ews):
        qs.append("What caused the cheque returns, and how is collections being managed?")
    if any(s["signal_type"] == "Document overdue" for s in ews):
        qs.append("When can the overdue stock statement and insurance renewal be provided?")
    return qs or ["Review account conduct and confirm renewal documents."]
