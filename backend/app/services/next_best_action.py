"""
backend/app/services/next_best_action.py

Relationship Strategy & Next-Best-Action — the flagship RM-assist use-case.

This replaces the weaker EWS / breach-radar / income-reconciliation analyst views
with a single, generative, branch-RM-grade play. It answers the question an Indian
MSME relationship manager actually has in front of a customer: *"Given everything we
know, what is the single best thing to do for THIS relationship right now, exactly how
do I say it, and what must I NOT promise?"*

It is deliberately AI-heavy: deterministic banking logic decides ELIGIBILITY and HARD
GATES (so we never upsell credit to a deteriorating account), and the LLM composes the
strategy, the India-aware talk-track and the guardrails on top of that gated evidence.
A deterministic fallback keeps the demo alive if Foundry is unreachable.
"""
from __future__ import annotations

import os

from app.store import DataStore

# Fastest chat model deployed in this environment (override via env). Falls back to the
# chat deployment injected by the infra (FOUNDRY_CHAT_DEPLOYMENT, set as a Container App
# env var), so this stays correct whatever the build names the deployment (e.g. gpt-4.1-mini-ptu/-payg).
FAST_CHAT_DEPLOYMENT = os.getenv("FAST_CHAT_DEPLOYMENT") or os.getenv("FOUNDRY_CHAT_DEPLOYMENT", "gpt-4.1-mini")

# Plain, customer-safe policy names — the model must never surface raw SOP filenames
# like "03_limit_enhancement_eligibility" to an RM (and certainly not to a customer).
SOP_FRIENDLY = {
    "01": "KYC / Re-KYC policy", "02": "Card Dispute & Chargeback policy",
    "03": "Loan Eligibility & FOIR policy", "04": "Unauthorised Transaction & Fraud policy",
    "05": "Collections & Restructuring policy", "06": "Insurance & Protection policy",
    "07": "Fair Practices & Grievance Redressal policy", "08": "Consent & DPDP policy",
    "09": "Escalation & Human Handoff policy",
}
from app.services.collateral import build_evidence_pack
from app.services.crosssell import opportunities
from app.services.analytics import AccountConduct, EnhancementAssessor
from app.services.relationship import crm_timeline


def _stance(conduct: dict, evidence: dict) -> str:
    trend = (conduct.get("credits_trend_label") or "").lower()
    chq = conduct.get("cheque_return_count") or 0
    if trend == "declining" or chq >= 3:
        return "Protect & Restructure"
    if trend == "rising" and chq <= 1:
        return "Grow & Deepen"
    return "Stabilise & Retain"


def _gated_offers(store: DataStore, cid: str) -> tuple[list[dict], list[dict]]:
    """Eligibility-gated cross/upsell. Eligible offers are growth plays; blocked ones
    become explicit do-not-offer items with the reason (e.g. Kaveri's OD enhancement)."""
    offers = opportunities(store, cid)
    eligible = [o for o in offers if o.get("eligible")]
    blocked = [o for o in offers if not o.get("eligible")]
    return eligible, blocked


def _open_cases(store: DataStore, cid: str) -> list[dict]:
    tl = crm_timeline(store, cid).get("events", [])
    out = []
    for e in tl:
        if e.get("type") in ("service", "interaction", "task") and \
           not str(e.get("status", "")).lower() in ("closed", "resolved", "completed", "saved"):
            out.append({"type": e.get("type"), "title": e.get("title"), "status": e.get("status")})
    return out[:5]


def _deterministic(stance: str, evidence: dict, enh: dict, eligible: list[dict],
                   blocked: list[dict], cases: list[dict]) -> dict:
    plays: list[dict] = []
    # growth play(s) from eligible offers
    for o in eligible[:2]:
        plays.append({
            "title": f"Offer {o.get('product')}",
            "type": "grow",
            "product": o.get("product"),
            "eligibility": "eligible",
            "the_number": enh.get("recommended_band_inr") if "enhanc" in (o.get("product", "").lower()) else "",
            "rationale": f"Fits signals: {', '.join(o.get('matched_signals', [])[:3])}.",
            "say": f"Given your {', '.join(o.get('matched_signals', [])[:1]) or 'profile'}, {o.get('product')} would suit you — shall I start it?",
            "guardrail": "Subject to standard appraisal; no rate/limit committed on the call.",
            "sop_basis": "Product eligibility & suitability SOP",
        })
    # protect / restructure play if stressed
    if stance != "Grow & Deepen":
        fac = evidence.get("facility", {})
        plays.append({
            "title": "Service recovery before any new credit",
            "type": "protect",
            "product": "Card EMI conversion / loan restructuring",
            "eligibility": "conditional",
            "the_number": fac.get("utilisation_avg_30d_pct"),
            "rationale": f"EMI/auto-debit bounces {evidence.get('stress', {}).get('cheque_returns_recent', 0)}; card utilisation {fac.get('utilisation_avg_30d_pct')}%.",
            "say": "Let's first sort the disputed transaction and ease the EMIs by converting the card balance — that protects your credit score and gives us a clean base.",
            "guardrail": "Do not offer a higher limit or new loan while the account is in collections / dispute.",
            "sop_basis": "Collections & Restructuring policy",
        })
    do_not = [{"product": o.get("product"), "reason": "; ".join(o.get("blocked_by", [])) or "eligibility not met"}
              for o in blocked[:3]]
    return {
        "stance": stance,
        "headline": f"{stance}: {len(eligible)} eligible play(s); {len(blocked)} suppressed.",
        "relationship_read": f"{evidence.get('company')} — card limit {evidence.get('facility', {}).get('sanction_limit_text')}, "
                             f"utilisation {evidence.get('facility', {}).get('utilisation_avg_30d_pct')}%, "
                             f"CIBIL {evidence.get('bureau', {}).get('score')}, "
                             f"{evidence.get('stress', {}).get('cheque_returns_recent', 0)} recent EMI/auto-debit bounces.",
        "plays": plays,
        "do_not_offer": do_not,
        "open_cases": cases,
        "generated_by": "deterministic_fallback",
    }


def next_best_action(store: DataStore, cid: str) -> dict:
    """The Next-Best-Action strategy for a customer. Deterministic gating + LLM compose."""
    from app.services import llm

    evidence = build_evidence_pack(store, cid)
    conduct = AccountConduct(store, cid).summary()
    enh = EnhancementAssessor(store, cid).assess()
    eligible, blocked = _gated_offers(store, cid)
    cases = _open_cases(store, cid)
    stance = _stance(conduct, evidence)

    fallback = _deterministic(stance, evidence, enh, eligible, blocked, cases)
    if not llm.available():
        return fallback

    gated = {
        "customer": evidence.get("company"),
        "stance_hint": stance,
        "facility": evidence.get("facility"),
        "turnover": evidence.get("turnover"),
        "stress": evidence.get("stress"),
        "conduct": {
            "credits_trend_label": conduct.get("credits_trend_label"),
            "credits_trend_pct": conduct.get("credits_trend_pct"),
            "avg_utilization_pct": conduct.get("avg_utilization_pct"),
            "cheque_return_count": conduct.get("cheque_return_count"),
        },
        "enhancement_assessment": enh,         # eligible? band? blockers?
        "eligible_offers": eligible,           # DETERMINISTICALLY gated — only these may be pitched
        "blocked_offers": blocked,             # MUST NOT be pitched; explain the gate instead
        "open_cases": cases,
        "top_buyer": evidence.get("top_buyer"),
    }
    task = (
        "You are an elite Indian-bank RETAIL Relationship Manager's strategy co-pilot — covering both the Branch RM and the "
        "Case-Dispute RM roles — for an INDIVIDUAL customer. "
        "Using ONLY the gated evidence, produce the single best relationship action RIGHT NOW, written like a sharp senior colleague briefing the RM — not a data dump. "
        "HARD RULES: never recommend a credit limit increase / new loan unless enhancement_assessment.eligible_for_review is true; "
        "you may ONLY pitch products listed in eligible_offers; for anything in blocked_offers, advise the RM to DECLINE it gracefully and say exactly what to fix first (cite the blocker). "
        "If there is an OPEN dispute or EMI bounce, SERVICE RECOVERY comes first (resolve the chargeback / offer EMI conversion or restructuring) — never upsell a stressed account. "
        "Be specific to Indian RETAIL banking (CIBIL score, card utilisation, FOIR, EMI bounces, chargebacks, KYC, SMA tags, RBI Fair Practices). "
        "WRITING RULES for every play: "
        "'rationale' = 2 short sentences of plain-English COACHING for the RM — the read on the situation and WHY this is the move (not just a metric). "
        "'say' = natural spoken words the RM can say to the CUSTOMER; it MUST NOT contain any policy code, SOP number/filename, internal jargon, or the word 'SOP'. "
        "'sop_basis' = a plain policy NAME for the RM's eyes only (e.g. 'Loan Eligibility & FOIR policy', 'Card Dispute & Chargeback policy'), NEVER a filename or code. "
        "'the_number' = the single most relevant figure (CIBIL score, limit, EMI, utilisation %, disputed amount). "
        "Lead with the most valuable, deliverable action; 2-4 plays max."
    )
    schema = (
        '{"stance":"Grow & Deepen|Protect & Restructure|Stabilise & Retain",'
        '"headline":"one punchy line the RM reads first",'
        '"relationship_read":"2-3 sentence plain-English read of where this relationship stands and the play",'
        '"plays":[{"title":"...","type":"grow|protect|retain|serve","product":"...",'
        '"eligibility":"eligible|conditional|blocked","the_number":"...",'
        '"rationale":"2 sentences of RM coaching: the read + why this move",'
        '"say":"natural customer-facing words, NO policy codes/SOP names",'
        '"guardrail":"what NOT to promise","sop_basis":"plain policy name, never a filename"}],'
        '"do_not_offer":[{"product":"...","reason":"..."}],'
        '"open_cases":[{"type":"...","title":"...","status":"..."}]}'
    )
    try:
        out = llm.narrate_json(task, gated, schema, temperature=0.45, max_tokens=1600,
                               deployment=FAST_CHAT_DEPLOYMENT)
        if not isinstance(out, dict) or "plays" not in out:
            return fallback
        out["generated_by"] = "llm_grounded"
        out.setdefault("open_cases", cases)
        out.setdefault("do_not_offer", fallback["do_not_offer"])
        out["customer_id"] = cid
        return out
    except Exception:
        return fallback
