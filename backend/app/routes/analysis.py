"""
backend/app/routes/analysis.py

The RM Assist analytical + CRM API (blueprint Section 12.5). All endpoints are
bearer-protected. Read endpoints return evidence-cited analysis; write endpoints
are approval-gated (propose -> approve) and emit audit events.
"""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.deps import require_bearer, get_store
from app.store import DataStore
from app.services.analytics import AccountConduct, EWSEngine, EnhancementAssessor
from app.services.portfolio import priority_queue, customer_360
from app.services.memo import MemoService
from app.services.relationship import relationship_dossier, recent_transactions, crm_timeline, live_call_playbook, query_transactions, transaction_insights
from app.services.command_center import command_center, credit_readiness, opportunity_workbench
from app.services.breach_radar import breach_radar, breach_simulate
from app.services.next_best_action import next_best_action
from app.services import card_limit

router = APIRouter(prefix="/v1", tags=["analysis"], dependencies=[Depends(require_bearer)])


@router.get("/portfolio/priority-queue")
def get_priority_queue(rm_id: str | None = None, store: DataStore = Depends(get_store)):
    return {"queue": priority_queue(store, rm_id)}


@router.get("/customers/{customer_id}/360")
def get_customer_360(customer_id: str, store: DataStore = Depends(get_store)):
    data = customer_360(store, customer_id)
    if not data:
        raise HTTPException(404, f"Customer {customer_id} not found")
    return data


@router.get("/customers/{customer_id}/command-center")
def get_command_center(customer_id: str, store: DataStore = Depends(get_store)):
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, f"Customer {customer_id} not found")
    return command_center(store, customer_id)


@router.get("/customers/{customer_id}/credit-readiness")
def get_credit_readiness(customer_id: str, store: DataStore = Depends(get_store)):
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, f"Customer {customer_id} not found")
    return credit_readiness(store, customer_id)


@router.get("/customers/{customer_id}/opportunity-workbench")
def get_opportunity_workbench(customer_id: str, store: DataStore = Depends(get_store)):
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, f"Customer {customer_id} not found")
    return opportunity_workbench(store, customer_id)


@router.get("/customers/{customer_id}/breach-radar")
def get_breach_radar(customer_id: str, store: DataStore = Depends(get_store)):
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, f"Customer {customer_id} not found")
    return breach_radar(store, customer_id)


class BreachScenario(BaseModel):
    buyer_payment_delay_inr: float = 0.0
    delay_days: int = 30
    sales_drop_pct: float = 0.0
    additional_drawdown_inr: float = 0.0


@router.post("/customers/{customer_id}/breach-radar/simulate")
def post_breach_simulate(customer_id: str, scenario: BreachScenario,
                         narrative: bool = True, store: DataStore = Depends(get_store)):
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, f"Customer {customer_id} not found")
    result = breach_simulate(store, customer_id, scenario.model_dump())
    if narrative:
        result["ai_narrative"] = _whatif_narrative(store, customer_id, result)
    return result


def _whatif_narrative(store: DataStore, customer_id: str, sim: dict) -> dict:
    """LLM layer over the deterministic what-if projection. The math computes the
    numbers (utilisation drift, cover ratio, days-to-breach, breach band) — auditable;
    the model explains, in plain RM language, what the scenario means and what to do.
    Grounded strictly in the simulation output; falls back to deterministic text."""
    from app.services import llm
    cust = store.one("customer_master", customer_id=customer_id) or {}
    sc = sim.get("scenario", {})
    proj = sim.get("projected", {})
    delta = sim.get("delta", {})
    # deterministic fallback
    crosses = proj.get("crosses_breach_line")
    fallback = (
        f"Under this scenario, projected utilisation moves to {proj.get('utilization_pct_after_window')}% "
        f"(change {delta.get('utilization_pct')} pts) and security cover to {proj.get('cover_ratio')}x. "
        f"Breach band: {proj.get('breach_band')}. "
        + ("This crosses the breach line — pre-emptive action is warranted." if crosses
           else "This stays within tolerance under the modelled assumptions.")
    )
    if not llm.available():
        return {"summary": fallback, "generated_by": "deterministic_fallback"}
    try:
        evidence = {
            "customer": {"display_name": cust.get("display_name"), "risk_category": cust.get("risk_category")},
            "scenario": sc,
            "baseline": sim.get("baseline", {}),
            "projected": proj,
            "delta": delta,
            "recommended_actions": sim.get("recommended_actions", []),
        }
        task = (
            "Explain this what-if stress scenario to the RM in plain language, grounded ONLY in the evidence. "
            "Cover: (1) what the scenario assumes (the inputs), in one sentence; "
            "(2) what happens to utilisation, security cover and the breach band, citing the projected numbers; "
            "(3) whether and roughly when it crosses the breach line; "
            "(4) the single most important pre-emptive step from recommended_actions. "
            "Be clear this is a projection under assumptions, not a prediction or a credit decision. "
            "Under 130 words. Do not invent numbers beyond the evidence."
        )
        text = llm.narrate(task, evidence, temperature=0.4, max_tokens=400)
        return {"summary": text, "generated_by": "llm_grounded", "crosses_breach_line": crosses}
    except Exception:
        return {"summary": fallback, "generated_by": "deterministic_fallback"}


@router.get("/customers/{customer_id}/relationship-dossier")
def get_relationship_dossier(customer_id: str, store: DataStore = Depends(get_store)):
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, f"Customer {customer_id} not found")
    return relationship_dossier(store, customer_id)


@router.get("/customers/{customer_id}/income-reconciliation")
def get_income_reconciliation(customer_id: str, narrative: bool = True,
                              store: DataStore = Depends(get_store)):
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, f"Customer {customer_id} not found")
    from app.services.income_reconciliation import income_reconciliation
    result = income_reconciliation(store, customer_id)
    if narrative:
        result["ai_narrative"] = _income_narrative(store, customer_id, result)
    return result


def _income_narrative(store: DataStore, customer_id: str, recon: dict) -> dict:
    """LLM analyst read over the deterministic triangulation. The engine computes the
    month-by-month GST/bank/turnover numbers and the divergence findings (auditable);
    the model explains what the divergences most plausibly mean and what the RM should
    ask — grounded strictly in the evidence, clarification-seeking, never alleging
    misreporting. Falls back to deterministic text if the LLM is unavailable."""
    from app.services import llm
    findings = recon.get("findings", [])
    agg = recon.get("aggregate", {})
    # deterministic fallback
    if findings:
        fallback = "; ".join(f"{x['finding_type']} ({x['severity']}): {x['evidence_metric']}" for x in findings)
    else:
        fallback = "Income sources broadly reconcile across the period."
    if not llm.available():
        return {"summary": fallback, "generated_by": "deterministic_fallback"}
    try:
        evidence = {
            "customer": {"display_name": recon.get("display_name"), "fy": recon.get("fy")},
            "audited_turnover_inr": recon.get("audited_turnover_inr"),
            "aggregate": agg,
            "findings": findings,
            # send a compact month view (not all fields) to keep the prompt tight
            "months": [{"period": m["period"], "gst_sales_inr": m["gst_sales_inr"],
                        "bank_credits_inr": m["bank_credits_inr"], "variance_pct": m["variance_pct"],
                        "direction": m["direction"], "cash_share_pct": m["cash_share_pct"],
                        "related_party_share_pct": m["related_party_share_pct"]}
                       for m in recon.get("months", [])],
        }
        task = (
            "You are reconciling three independent revenue measures for an MSME before a credit review: "
            "GST-declared sales, bank credits, and audited turnover. Using ONLY the evidence, write a concise "
            "analyst read for the RM: (1) one-sentence verdict on income quality / how well the three tie out; "
            "(2) the most material divergence(s) and the most plausible BENIGN-and-RISK explanations side by side "
            "(e.g. timing vs under-banking of cash vs related-party rotation) — use clarification-seeking language, "
            "never allege misreporting; (3) the exact documents/questions the RM should ask to close the gap before "
            "the review. Under 150 words. Do not invent any figure not in the evidence."
        )
        text = llm.narrate(task, evidence, temperature=0.4, max_tokens=460)
        return {"summary": text, "generated_by": "llm_grounded", "finding_count": len(findings)}
    except Exception:
        return {"summary": fallback, "generated_by": "deterministic_fallback"}


class TransactionQueryBody(BaseModel):
    operation: str = "recent"
    direction: str = "all"
    limit: int = 5
    period_days: int | None = None
    merchant: str | None = None
    category: str | None = None


@router.get("/customers/{customer_id}/transactions/insights")
def get_transaction_insights(customer_id: str, store: DataStore = Depends(get_store)):
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, f"Customer {customer_id} not found")
    return transaction_insights(store, customer_id)


@router.post("/customers/{customer_id}/transactions/query")
def post_transaction_query(customer_id: str, body: TransactionQueryBody, store: DataStore = Depends(get_store)):
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, f"Customer {customer_id} not found")
    return query_transactions(store, customer_id, operation=body.operation, direction=body.direction,
                              limit=body.limit, period_days=body.period_days,
                              merchant=body.merchant, category=body.category)


@router.get("/customers/{customer_id}/transactions/recent")
def get_recent_transactions(customer_id: str, limit: int = 12, store: DataStore = Depends(get_store)):
    return {"customer_id": customer_id, "transactions": recent_transactions(store, customer_id, limit)}


@router.get("/customers/{customer_id}/crm-timeline")
def get_crm_timeline(customer_id: str, store: DataStore = Depends(get_store)):
    return crm_timeline(store, customer_id)


@router.get("/customers/{customer_id}/live-call-playbook")
def get_live_call_playbook(customer_id: str, store: DataStore = Depends(get_store)):
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, f"Customer {customer_id} not found")
    return live_call_playbook(store, customer_id)


@router.post("/analysis/account-conduct")
def account_conduct(body: dict, store: DataStore = Depends(get_store)):
    cid = body.get("customer_id")
    if not cid:
        raise HTTPException(400, "customer_id required")
    return AccountConduct(store, cid).summary()


@router.get("/customers/{customer_id}/ews")
def get_ews(customer_id: str, narrative: bool = True, store: DataStore = Depends(get_store)):
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, f"Customer {customer_id} not found")
    signals = EWSEngine(store, customer_id).signals()
    result = {"customer_id": customer_id, "signals": signals}
    if narrative:
        result["ai_narrative"] = _ews_narrative(store, customer_id, signals)
    return result


def _ews_narrative(store: DataStore, customer_id: str, signals: list[dict]) -> dict:
    """LLM layer over the deterministic EWS signals. The rules decide WHAT fired and
    the severity (auditable); the model explains WHY it matters and what the RM should
    do, grounded strictly in the signals. Falls back to deterministic text if the LLM
    is unavailable."""
    from app.services import llm
    cust = store.one("customer_master", customer_id=customer_id) or {}
    evidence = {
        "customer": {
            "display_name": cust.get("display_name"),
            "segment": cust.get("segment"),
            "risk_category": cust.get("risk_category"),
            "relationship_value_score": cust.get("relationship_value_score"),
        },
        "signals": signals,
        "signal_count": len(signals),
        "highest_severity": ("Critical" if any(s["severity"] == "Critical" for s in signals)
                             else "High" if any(s["severity"] == "High" for s in signals)
                             else "Medium" if signals else "None"),
    }
    # deterministic fallback summary
    if signals:
        fallback = "; ".join(f"{s['signal_type']} ({s['severity']}): {s['evidence_metric']}" for s in signals)
    else:
        fallback = "No early-warning signals fired on current data; continue routine monitoring."
    if not signals or not llm.available():
        return {"summary": fallback, "generated_by": "deterministic_fallback"}
    try:
        task = (
            "Write a concise early-warning briefing for the RM based ONLY on the EWS signals in the evidence. "
            "Structure: (1) one-sentence overall read of the account's risk posture; "
            "(2) for each signal, one line explaining in plain language why it matters and the clarification to seek "
            "(use clarification-seeking language, never allege wrongdoing); "
            "(3) a single prioritised next step. Keep it under 140 words. Do not invent any numbers not in the evidence."
        )
        text = llm.narrate(task, evidence, temperature=0.4, max_tokens=420)
        return {"summary": text, "generated_by": "llm_grounded", "evidence_signal_count": len(signals)}
    except Exception:
        return {"summary": fallback, "generated_by": "deterministic_fallback"}


@router.get("/customers/{customer_id}/enhancement")
def get_enhancement(customer_id: str, store: DataStore = Depends(get_store)):
    return EnhancementAssessor(store, customer_id).assess()


@router.get("/customers/{customer_id}/card-limit-assessment")
def get_card_limit_assessment(customer_id: str, store: DataStore = Depends(get_store)):
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, f"Customer {customer_id} not found")
    return card_limit.assess(store, customer_id)


class CardLimitReviewBody(BaseModel):
    requested_limit_inr: float | None = None
    actor: str = "RM-2207"
    customer_consent: bool = False


@router.post("/customers/{customer_id}/card-limit-review")
def initiate_card_limit_review(customer_id: str, body: CardLimitReviewBody, store: DataStore = Depends(get_store)):
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, f"Customer {customer_id} not found")
    if not body.customer_consent:
        raise HTTPException(422, "Explicit customer consent is required before a card-limit review task can be created")
    result = card_limit.initiate_review(store, customer_id, body.requested_limit_inr, body.actor)
    if result.get("status") == "BLOCKED":
        return result
    return result


@router.get("/customers/{customer_id}/next-best-action")
def get_next_best_action(customer_id: str, store: DataStore = Depends(get_store)):
    """Relationship Strategy & Next-Best-Action — the flagship RM use-case. Eligibility
    is hard-gated deterministically; the LLM composes the strategy, talk-track and
    guardrails (e.g. decline an OD enhancement for a deteriorating account)."""
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, f"Customer {customer_id} not found")
    return next_best_action(store, customer_id)


@router.post("/memo/renewal-draft")
def memo_draft(body: dict, store: DataStore = Depends(get_store)):
    cid = body.get("customer_id")
    if not cid:
        raise HTTPException(400, "customer_id required")
    memo_type = body.get("memo_type", "renewal")
    return MemoService(store, cid).draft(memo_type)


# ---------- approval-gated CRM write-back ----------
class WriteCandidate(BaseModel):
    customer_id: str
    type: str                 # task | note | opportunity | ews_status
    payload: dict
    evidence_refs: list[str] = []


@router.post("/crm/update-candidate")
def propose_update(cand: WriteCandidate, store: DataStore = Depends(get_store)):
    # Guardrail: never accept a credit-approval status write.
    blob = str(cand.payload).lower()
    if any(w in blob for w in ("approved", "sanctioned", "credit approved")):
        raise HTTPException(422, "Refused: assistant cannot write credit-approval status (human-in-the-loop).")
    return store.propose_write(cand.model_dump())


class ApproveBody(BaseModel):
    candidate_id: str
    approver: str
    edited_payload: dict | None = None


@router.post("/crm/approve-update")
def approve_update(body: ApproveBody, store: DataStore = Depends(get_store)):
    saved = store.approve_write(body.candidate_id, body.approver, body.edited_payload)
    if not saved:
        raise HTTPException(404, "Candidate not found or already processed")
    return saved


@router.get("/crm/pending")
def list_pending(store: DataStore = Depends(get_store)):
    return {"pending": [c for c in store.pending_writes if c["status"] == "pending_approval"]}


# ---------- audit (glass-box) ----------
@router.get("/audit/events")
def audit_events(limit: int = 100, store: DataStore = Depends(get_store)):
    return {"events": store.events[-limit:]}


@router.get("/audit/{object_id}")
def audit_trace(object_id: str, store: DataStore = Depends(get_store)):
    hits = [e for e in store.events if object_id in str(e.get("payload", {}))]
    return {"object_id": object_id, "events": hits}
