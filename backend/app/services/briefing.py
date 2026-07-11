"""
backend/app/services/briefing.py

Start-of-day briefing (your point 2). For each customer in the RM's portfolio,
produces a narrative "what to discuss today" that weaves:
  - why this customer is prioritized (bucket + reason)
  - last CRM interaction + any open ticket
  - recent transaction/conduct signals
  - recommended talking points
  - eligibility-checked cross-sell/upsell angle

Every narrative line carries a structured REASONING TRACE (the data points / SOP
refs that produced it) so the RM can drill into the "why" (point 2.2). The trace
is deterministic and auditable; optional prose synthesis can sit on top later.
"""
from __future__ import annotations
from app.store import DataStore
from app.services.analytics import AccountConduct, EWSEngine, EnhancementAssessor
from app.services.crosssell import opportunities
from app.services.portfolio import priority_queue


def _trace(claim: str, evidence: list[str], source: str) -> dict:
    return {"claim": claim, "evidence": evidence, "source": source}


def customer_brief(store: DataStore, customer_id: str) -> dict:
    cust = store.one("customer_master", customer_id=customer_id) or {}
    conduct = AccountConduct(store, customer_id).summary()
    ews = EWSEngine(store, customer_id).signals()
    enh = EnhancementAssessor(store, customer_id).assess()
    opps = opportunities(store, customer_id)

    interactions = store.where("interactions", customer_id=customer_id)
    last_int = interactions[-1] if interactions else None
    open_srs = [s for s in store.where("service_requests", customer_id=customer_id) if s["status"] == "Open"]
    docs_pending = [d for d in store.where("documents", customer_id=customer_id)
                    if d["status"] in ("Pending", "Expired") and d["required_flag"] == "Y"]

    critical = [s for s in ews if s["severity"] == "Critical"]
    high = [s for s in ews if s["severity"] == "High"]

    # ---- build narrative lines, each with a reasoning trace ----
    lines: list[dict] = []

    # 1. headline / why prioritized
    if critical or len(high) >= 2:
        headline = f"Risk-first conversation. {len(high)} high and {len(critical)} critical signal(s) need addressing before any commercial discussion."
        lines.append(_trace(headline,
            [f"{s['signal_type']} ({s['severity']}): {s['evidence_metric']}" for s in ews if s["severity"] in ("High", "Critical")],
            "EWSEngine"))
    elif enh["eligible_for_review"]:
        headline = "Growth conversation. Account conduct supports initiating an enhancement review with conditions."
        lines.append(_trace(headline,
            [f"credits {conduct['credits_trend_label']} {conduct['credits_trend_pct']}%",
             f"utilization avg {conduct['avg_utilization_pct']}%",
             f"cheque returns {conduct['cheque_return_count']}"],
            "EnhancementAssessor"))
    else:
        headline = "Renewal review. Prepare standard renewal brief and confirm documents."
        lines.append(_trace(headline, [f"review due {store.one('facilities', customer_id=customer_id, ).get('review_due_date','-') if store.one('facilities', customer_id=customer_id) else '-'}"], "facilities"))

    # 2. last interaction continuity
    if last_int:
        lines.append(_trace(
            f"Continuity: last contact on {last_int['interaction_date']} ({last_int['channel']}) — \"{last_int['summary']}\". Customer commitment: {last_int['commitments_by_customer']}; bank commitment: {last_int['commitments_by_bank']}.",
            [f"interaction {last_int['interaction_id']}", f"sentiment {last_int.get('sentiment','-')}"],
            "rm_interactions"))

    # 3. open service tickets (service recovery before sell)
    if open_srs:
        sr = open_srs[0]
        lines.append(_trace(
            f"Service recovery first: open ticket {sr['ticket_id']} ({sr['category']}, {sr['customer_sentiment']} sentiment). Acknowledge before commercial topics.",
            [f"ticket {sr['ticket_id']}", f"priority {sr['priority']}", f"raised {sr['created_date']}"],
            "service_requests"))

    # 4. recent transaction signal
    txn_signal = _recent_txn_signal(store, customer_id, conduct)
    if txn_signal:
        lines.append(txn_signal)

    # 5. documents to collect
    if docs_pending:
        names = ", ".join(sorted({d["document_type"] for d in docs_pending}))
        blocking = [d["document_type"] for d in docs_pending if d.get("blocking_flag") == "Y"]
        lines.append(_trace(
            f"Documents to collect: {names}." + (f" BLOCKING: {', '.join(blocking)}." if blocking else ""),
            [f"{d['document_type']}: {d['status']}" for d in docs_pending],
            "document_status"))

    # 6. cross-sell / upsell angle (top eligible opportunity)
    eligible_opps = [o for o in opps if o["eligible"]]
    if eligible_opps:
        top = eligible_opps[0]
        lines.append(_trace(
            f"Opportunity: {top['product']} — {top['rationale']}",
            ["matched: " + ", ".join(top["matched_signals"]), f"maps to SOP {top['sop_ref']}"],
            "CrossSellEngine"))
    elif critical:
        lines.append(_trace(
            "No cross-sell today — stabilize conduct and clear blockers first.",
            [f"critical signal: {critical[0]['signal_type']}"],
            "CrossSellEngine"))

    # talking points (concise, ordered)
    talking_points = [ln["claim"] for ln in lines]

    return {
        "customer_id": customer_id,
        "display_name": cust.get("display_name"),
        "narrative_lines": lines,            # each with claim + evidence + source (drill-down)
        "talking_points": talking_points,
        "cross_sell": eligible_opps[:3],
        "blocked_opportunities": [o for o in opps if not o["eligible"]][:3],
        "headline": lines[0]["claim"] if lines else "",
    }


def _recent_txn_signal(store: DataStore, customer_id: str, conduct: dict) -> dict | None:
    """Surface the most recent notable transaction-level fact for continuity."""
    txns = store.where("transactions", customer_id=customer_id)
    if not txns:
        return None
    # last cheque return, if any
    returns = [t for t in txns if t.get("is_return") == "Y"]
    if returns:
        last = sorted(returns, key=lambda t: t["txn_date"])[-1]
        return _trace(
            f"Recent conduct flag: cheque return on {last['txn_date']} ({last['return_reason']}, {last['counterparty_name']}). Ask for context — do not allege wrongdoing.",
            [f"txn {last['txn_id']}", f"reason {last['return_reason']}"],
            "transactions")
    # else a recent high-value credit (positive talking point)
    big = [t for t in txns if t.get("dr_cr") == "CR" and float(t["amount_inr"]) > 1_500_000]
    if big:
        last = sorted(big, key=lambda t: t["txn_date"])[-1]
        return _trace(
            f"Recent positive: large receipt {last['counterparty_name']} on {last['txn_date']}. Good entry point to discuss pipeline.",
            [f"txn {last['txn_id']}", f"amount {last['amount_inr']}"],
            "transactions")
    return None


def daily_briefing(store: DataStore, rm_id: str | None = None) -> dict:
    """Whole-portfolio briefing in priority order."""
    queue = priority_queue(store, rm_id)
    briefs = [customer_brief(store, c["customer_id"]) for c in queue]
    return {
        "rm_id": rm_id or "RM-1042",
        "generated_for": "today",
        "customer_count": len(briefs),
        "briefs": briefs,
    }
