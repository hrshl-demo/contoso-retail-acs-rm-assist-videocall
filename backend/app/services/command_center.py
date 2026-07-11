"""
Command-center services for the Contoso MSME RM Assist POC.

These helpers deliberately remain deterministic and evidence-led. The LLM layer
can narrate on top, but the cockpit widgets and live-call guardrails are driven
from the frozen CSV facts loaded in DataStore.
"""
from __future__ import annotations
from datetime import datetime, timedelta
from app.store import DataStore
from app.services.analytics import AccountConduct, EWSEngine, EnhancementAssessor
from app.services.crosssell import opportunities
from app.services.relationship import recent_transactions, crm_timeline


def _safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def _days_until(date_s: str) -> int | None:
    try:
        d = datetime.fromisoformat(str(date_s)[:10]).date()
        return (d - datetime.utcnow().date()).days
    except Exception:
        return None


def _pending_docs(store: DataStore, customer_id: str) -> list[dict]:
    rows = []
    for d in store.where("documents", customer_id=customer_id):
        status = (d.get("status") or "").lower()
        if status in {"pending", "expired", "overdue"} or d.get("blocking_flag") == "Y":
            rows.append(d)
    return rows


def credit_readiness(store: DataStore, customer_id: str) -> dict:
    cust = store.one("customer_master", customer_id=customer_id) or {}
    conduct = AccountConduct(store, customer_id).summary()
    ews = EWSEngine(store, customer_id).signals()
    enh = EnhancementAssessor(store, customer_id).assess()
    docs = _pending_docs(store, customer_id)
    service_open = [s for s in store.where("service_requests", customer_id=customer_id) if s.get("status") == "Open"]
    facility = store.one("facilities", customer_id=customer_id) or {}

    score = 50
    pos, blockers, guardrails = [], [], []
    if conduct.get("credits_trend_label") == "rising":
        score += 18; pos.append(f"Bank credits are rising {conduct.get('credits_trend_pct')}% over the year.")
    else:
        score -= 15; blockers.append(f"Bank credits are {conduct.get('credits_trend_label')} {conduct.get('credits_trend_pct')}%.")
    if conduct.get("avg_utilization_pct", 0) >= 75:
        score += 10; pos.append(f"Average utilization is {conduct.get('avg_utilization_pct')}%, supporting a working-capital conversation.")
    if conduct.get("cheque_return_count", 0) <= 1:
        score += 8; pos.append("Cheque-return conduct is broadly clean.")
    else:
        score -= min(22, conduct.get("cheque_return_count", 0) * 4); blockers.append(f"{conduct.get('cheque_return_count')} cheque returns need clarification before growth messaging.")

    critical = [s for s in ews if s.get("severity") == "Critical"]
    high = [s for s in ews if s.get("severity") == "High"]
    score -= len(critical) * 18 + len(high) * 8
    for s in critical + high:
        blockers.append(f"{s.get('severity')} EWS: {s.get('signal_type')} - {s.get('evidence_metric')}")

    blocking_docs = [d for d in docs if d.get("blocking_flag") == "Y" or d.get("status") in {"Expired", "Overdue"}]
    score -= len(blocking_docs) * 7
    for d in blocking_docs:
        blockers.append(f"Document blocker: {d.get('document_type')} is {d.get('status')}.")

    if service_open:
        score -= min(15, len(service_open) * 5)
        blockers.append(f"{len(service_open)} open service issue(s) should be acknowledged before product pitching.")

    if not enh.get("eligible_for_review"):
        guardrails.append("Do not position this as an approval or sanction. Frame as remediation/review subject to credit appraisal.")
    else:
        guardrails.append("Enhancement can be positioned only as a review subject to credit appraisal and document validation.")

    score = max(0, min(100, score))
    if score >= 75:
        label = "High readiness - proceed with document-backed review"
    elif score >= 55:
        label = "Moderate readiness - pursue with blockers visible"
    elif score >= 35:
        label = "Low readiness - remediate before credit growth"
    else:
        label = "Caution - service/risk first, no enhancement messaging"

    return {
        "customer_id": customer_id,
        "score": score,
        "label": label,
        "positive_factors": pos[:6],
        "blockers": blockers[:8],
        "guardrails": guardrails,
        "facility_review_due": facility.get("review_due_date", ""),
        "relationship_value_score": cust.get("relationship_value_score", ""),
        "evidence_refs": ["analytics:account_conduct", "ews_engine", "document_status", "service_requests", "enhancement_assessor"],
    }


def opportunity_workbench(store: DataStore, customer_id: str) -> dict:
    engine_opps = opportunities(store, customer_id)
    crm_opps = store.where("opportunities", customer_id=customer_id)
    lanes = {"Identified": [], "Discussed": [], "Documents pending": [], "Under review": [], "Converted": [], "Blocked": []}

    for o in crm_opps:
        stage = (o.get("stage") or o.get("status") or "Identified").lower()
        item = {
            "source": "CRM",
            "id": o.get("opportunity_id"),
            "title": o.get("opportunity_type"),
            "stage": o.get("stage"),
            "status": o.get("status"),
            "recommended_band_inr": o.get("recommended_band_inr"),
            "blockers": [b.strip() for b in (o.get("blockers") or "").split(";") if b.strip()],
        }
        if "doc" in stage or "pending" in stage:
            lanes["Documents pending"].append(item)
        elif "review" in stage or "rm discussion" in stage:
            lanes["Under review"].append(item)
        elif "convert" in stage or o.get("status") == "Converted":
            lanes["Converted"].append(item)
        elif item["blockers"] or o.get("status") in {"Blocked", "Hold"}:
            lanes["Blocked"].append(item)
        elif "discuss" in stage:
            lanes["Discussed"].append(item)
        else:
            lanes["Identified"].append(item)

    for o in engine_opps:
        item = {
            "source": "RM Assist",
            "id": o.get("product_id"),
            "title": o.get("product"),
            "category": o.get("category"),
            "stage": "Identified" if o.get("eligible") else "Blocked",
            "status": o.get("stance"),
            "blockers": o.get("blocked_by") or [],
            "matched_signals": o.get("matched_signals") or [],
            "rationale": o.get("rationale"),
            "sop_ref": o.get("sop_ref"),
        }
        lanes["Identified" if o.get("eligible") else "Blocked"].append(item)

    return {"customer_id": customer_id, "lanes": lanes, "summary": {k: len(v) for k, v in lanes.items()}}


def command_center(store: DataStore, customer_id: str) -> dict:
    cust = store.one("customer_master", customer_id=customer_id) or {}
    profile = store.one("business_profile", customer_id=customer_id) or {}
    conduct = AccountConduct(store, customer_id).summary()
    ews = EWSEngine(store, customer_id).signals()
    enh = EnhancementAssessor(store, customer_id).assess()
    readiness = credit_readiness(store, customer_id)
    opp_wb = opportunity_workbench(store, customer_id)
    docs = _pending_docs(store, customer_id)
    open_srs = [s for s in store.where("service_requests", customer_id=customer_id) if s.get("status") == "Open"]
    threads = store.where("engagement_threads", customer_id=customer_id)

    if enh.get("eligible_for_review"):
        thesis = (f"Growth case with controlled credit readiness. {cust.get('display_name')} shows {conduct.get('credits_trend_label')} bank credits, "
                  f"average utilization of {conduct.get('avg_utilization_pct')}%, and a commercially valid credit discussion. The RM should pursue review, "
                  "but only after document and buyer/payment-cycle evidence is captured.")
    elif any(s.get("severity") == "Critical" for s in ews):
        thesis = (f"Caution case requiring service/risk sequencing. {cust.get('display_name')} has critical blockers that should be addressed before growth pitching. "
                  "The RM should lead with service recovery, conduct clarification, and document remediation.")
    else:
        thesis = (f"Renewal and relationship-deepening case. {cust.get('display_name')} needs a structured review conversation with product opportunities handled after basic checks.")

    missions = []
    def add_mission(kind, title, why, outcome, severity="Medium", refs=None):
        missions.append({"kind": kind, "title": title, "why_now": why, "target_outcome": outcome, "severity": severity, "evidence_refs": refs or []})

    if enh.get("eligible_for_review"):
        add_mission("Growth", "Explore working-capital enhancement review", f"Credits {conduct.get('credits_trend_label')} {conduct.get('credits_trend_pct')}% and utilization is {conduct.get('avg_utilization_pct')}%.", "Create opportunity and document checklist; do not commit approval.", "High", ["enhancement_assessor", "utilization"])
    for s in ews[:3]:
        add_mission("Risk", f"Clarify {s.get('signal_type')}", s.get("evidence_metric", ""), s.get("recommended_action", ""), s.get("severity", "Medium"), s.get("evidence_refs", []))
    if docs:
        add_mission("Documents", "Close credit-document blockers", ", ".join(sorted(set(d.get("document_type", "") for d in docs[:4]))), "Request documents and create verification tasks.", "High", ["document_status"])
    if open_srs:
        add_mission("Service", "Acknowledge open service issue before selling", open_srs[0].get("description", "Open service request"), "Create/confirm service recovery owner and timeline.", "High", ["service_requests"])
    for t in threads[:3]:
        add_mission("Thread", t.get("topic", "Engagement thread"), t.get("angle", ""), t.get("products", ""), t.get("priority", "Medium"), ["engagement_threads"])

    sequence = []
    if open_srs:
        sequence.append({"step": 1, "label": "Service recovery first", "instruction": "Acknowledge the open ticket or customer dissatisfaction before asking for commercial action.", "why": "Service pain can derail product or credit conversations."})
    sequence.append({"step": len(sequence)+1, "label": "Confirm business trigger", "instruction": "Ask what changed: order, buyer delay, renewal timeline, or document dispute.", "why": "The assistant should anchor the conversation to the customer's stated trigger."})
    sequence.append({"step": len(sequence)+1, "label": "Capture measurable facts", "instruction": "Capture amount, buyer, payment cycle, delivery date, promised documents, and urgency.", "why": "These become CRM facts and credit handoff evidence."})
    sequence.append({"step": len(sequence)+1, "label": "Frame review boundaries", "instruction": "Use review-subject-to-credit language; avoid approval or sanction commitments.", "why": "RM Assist must protect credit and compliance guardrails."})
    sequence.append({"step": len(sequence)+1, "label": "Create CRM action", "instruction": "Convert the nudge to a CRM note, task, opportunity, or document request before ending the call.", "why": "The POC must show live-call insight becoming operational follow-through."})

    return {
        "customer_id": customer_id,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "relationship_thesis": thesis,
        "customer": {"display_name": cust.get("display_name"), "segment": cust.get("segment"), "risk_category": cust.get("risk_category"), "rvs": cust.get("relationship_value_score")},
        "credit_readiness": readiness,
        "today_missions": missions[:8],
        "recommended_sequence": sequence,
        "opportunity_workbench": opp_wb,
        "document_pack": {
            "pending": [{"document_type": d.get("document_type"), "status": d.get("status"), "blocking_flag": d.get("blocking_flag"), "remarks": d.get("remarks")} for d in docs],
            "customer_request_template": [d.get("document_type") for d in docs[:5]] or ["Latest GST return", "Latest stock statement", "Debtor aging statement"],
        },
        "risk_posture": {"max_severity": ews[0].get("severity") if ews else "None", "signals": ews},
        "recent_transactions": recent_transactions(store, customer_id, 8),
        "timeline_preview": (crm_timeline(store, customer_id).get("events", [])[:6]),
        "evidence_refs": ["customer_master", "business_profile", "account_conduct", "ews_engine", "documents", "engagement_threads", "opportunities"],
    }
