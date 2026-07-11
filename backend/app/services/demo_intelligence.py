"""
Dynamic RM Assist demo intelligence for the CRM cockpit.

This layer converts deterministic customer facts into click-ready AI surfaces:
- briefing studio: relationship thesis + daily briefing as dynamic cards/drilldowns
- breach/income copilot: a demoable risk-control-tower journey over breach radar
  and GST/bank/turnover reconciliation.

The engine is evidence-first. LLM output enriches wording and sequencing only;
all figures come from the synthetic customer data loaded by DataStore.
"""
from __future__ import annotations

from app.store import DataStore
from app.services import llm
from app.services.analytics import AccountConduct, EWSEngine, EnhancementAssessor
from app.services.breach_radar import breach_radar, breach_simulate
from app.services.income_reconciliation import income_reconciliation
from app.services.crosssell import opportunities
from app.services.relationship import crm_timeline, recent_transactions, _top_counterparties
from app.services.command_center import command_center


def _f(v, d=0.0):
    try:
        return float(v) if v not in (None, "") else d
    except Exception:
        return d


def _inr(n) -> str:
    n = _f(n)
    if abs(n) >= 1e7:
        return f"₹{n/1e7:.2f} Cr"
    if abs(n) >= 1e5:
        return f"₹{n/1e5:.1f} L"
    return f"₹{n:,.0f}"


def _facts(store: DataStore, customer_id: str) -> dict:
    cust = store.one("customer_master", customer_id=customer_id) or {}
    prof = store.one("business_profile", customer_id=customer_id) or {}
    facility = store.one("facilities", customer_id=customer_id) or {}
    conduct = AccountConduct(store, customer_id).summary()
    ews = EWSEngine(store, customer_id).signals()
    enh = EnhancementAssessor(store, customer_id).assess()
    opps = opportunities(store, customer_id)
    docs = [d for d in store.where("documents", customer_id=customer_id)
            if d.get("status") in ("Pending", "Expired", "Overdue") or d.get("blocking_flag") == "Y"]
    srs = [s for s in store.where("service_requests", customer_id=customer_id) if s.get("status") == "Open"]
    aging = store.one("aging", customer_id=customer_id) or {}
    fin = store.one("financials", customer_id=customer_id) or {}
    tops = _top_counterparties(store, customer_id, 5)
    interactions = (crm_timeline(store, customer_id).get("events") or [])[:8]
    txns = recent_transactions(store, customer_id, 8)
    return {
        "customer": cust, "profile": prof, "facility": facility, "conduct": conduct,
        "ews": ews, "enhancement": enh, "opportunities": opps, "documents": docs,
        "service_requests": srs, "aging": aging, "financials": fin,
        "top_counterparties": tops, "recent_interactions": interactions,
        "recent_transactions": txns,
    }


def _briefing_fallback(e: dict, customer_id: str) -> dict:
    cust, fac, conduct = e["customer"], e["facility"], e["conduct"]
    ews, docs, srs = e["ews"], e["documents"], e["service_requests"]
    opps = [o for o in e["opportunities"] if o.get("eligible")]
    stressed = any(x.get("severity") in ("Critical", "High") for x in ews) or conduct.get("credits_trend_pct", 0) < 0
    posture = "Stabilise" if stressed else ("Grow" if opps else "Watch")
    headline = f"{cust.get('display_name')} is a {posture.lower()} conversation: credits {conduct.get('credits_trend_label')} {conduct.get('credits_trend_pct')}%, utilisation {conduct.get('avg_utilization_pct')}%, {len(docs)} document blocker(s)."
    primary = []
    if ews:
        primary.append({"id": "risk", "title": "Risk first", "question": "What can derail the review?",
                        "answer": f"{ews[0].get('signal_type')} — {ews[0].get('evidence_metric')}",
                        "evidence": ["ews", "account_conduct"],
                        "cta": "Open risk reasoning", "priority": "High"})
    if opps:
        primary.append({"id": "growth", "title": "Growth angle", "question": "Where can the RM create value?",
                        "answer": f"Best eligible offer: {opps[0].get('product')} — subject to policy and consent.",
                        "evidence": ["product_rules", "conduct", "consent"],
                        "cta": "Generate pitch", "priority": "Medium"})
    if docs or srs:
        primary.append({"id": "blockers", "title": "Blockers", "question": "What must be closed before pitching?",
                        "answer": f"{len(docs)} document item(s) and {len(srs)} open service issue(s) should be sequenced before any aggressive product conversation.",
                        "evidence": ["documents", "service_requests"],
                        "cta": "Create closure plan", "priority": "High"})
    primary.append({"id": "opening", "title": "Opening line", "question": "How should the RM start?",
                    "answer": "Lead with the customer’s immediate business context, then ask for clarification on the most material blocker. Do not promise sanction.",
                    "evidence": ["crm_timeline", "facility", "sop"], "cta": "Show talk track", "priority": "Medium"})
    return {
        "customer_id": customer_id,
        "generated_by": "deterministic_fallback",
        "headline": headline,
        "posture": posture,
        "briefing_cards": primary,
        "demo_prompts": [
            "Why is this customer on my briefing today?",
            "What should I ask first on the call?",
            "Which blocker must be handled before cross-sell?",
            "What can I safely say without implying approval?",
        ],
        "meeting_sequence": [
            {"step": 1, "label": "Acknowledge", "instruction": "Start with the business context and prior CRM case."},
            {"step": 2, "label": "Clarify", "instruction": "Ask for the one missing fact that changes the credit posture."},
            {"step": 3, "label": "Act", "instruction": "Create task/opportunity/document request only after clarification."},
        ],
        "evidence_footprint": {"signals": len(ews), "open_cases": len(srs), "document_blockers": len(docs), "eligible_offers": len(opps)},
        "guardrail": "Internal RM guidance only; do not imply sanction or accuse the customer.",
    }


def briefing_studio(store: DataStore, customer_id: str) -> dict:
    e = _facts(store, customer_id)
    fallback = _briefing_fallback(e, customer_id)
    if not llm.available():
        return fallback
    evidence = {
        "customer": {"name": e["customer"].get("display_name"), "segment": e["customer"].get("segment"),
                     "risk": e["customer"].get("risk_category"), "rvs": e["customer"].get("relationship_value_score")},
        "facility": {"type": e["facility"].get("facility_type"), "limit": e["facility"].get("sanction_limit_inr"),
                     "outstanding": e["facility"].get("current_outstanding_inr"),
                     "review_due": e["facility"].get("review_due_date")},
        "conduct": e["conduct"],
        "early_warning": [{"signal": x.get("signal_type"), "severity": x.get("severity"), "evidence": x.get("evidence_metric")} for x in e["ews"]],
        "documents": [{"doc": x.get("document_type"), "status": x.get("status"), "blocking": x.get("blocking_flag")} for x in e["documents"]],
        "service_requests": [{"category": x.get("category"), "priority": x.get("priority"), "summary": x.get("summary")} for x in e["service_requests"]],
        "opportunities": [{"product": x.get("product"), "eligible": x.get("eligible"), "rationale": x.get("rationale")} for x in e["opportunities"]],
        "top_counterparties": e["top_counterparties"],
        "recent_interactions": e["recent_interactions"][:5],
        "fallback_shape": fallback,
    }
    schema = (
        '{"headline":"one dynamic morning-briefing headline",'
        '"posture":"Grow|Protect|Stabilise|Watch",'
        '"briefing_cards":[{"id":"short id","title":"card title","question":"question this card answers",'
        '"answer":"specific answer grounded in evidence","evidence":["source chips"],"cta":"button label","priority":"High|Medium|Low"}],'
        '"demo_prompts":["clickable RM questions"],'
        '"meeting_sequence":[{"step":1,"label":"stage","instruction":"what RM does"}],'
        '"guardrail":"one sentence"}'
    )
    task = (
        "Create a dynamic start-of-day RM briefing studio for this MSME customer. It must feel like an AI analyst, "
        "not a static CRM summary. Generate 5-6 click-ready cards. Each card answers a different RM question: why now, "
        "risk, growth, blocker, next question, and safe customer language. Be specific to the evidence. Use exact numbers "
        "when available. Do not invent facts. Never promise approval or allege wrongdoing."
    )
    try:
        out = llm.narrate_json(task, evidence, schema, temperature=0.5, max_tokens=1700)
        out["customer_id"] = customer_id
        out["generated_by"] = "llm_grounded"
        out["evidence_footprint"] = fallback["evidence_footprint"]
        return out
    except Exception:
        return fallback


def briefing_drilldown(store: DataStore, customer_id: str, card_id: str, question: str = "") -> dict:
    e = _facts(store, customer_id)
    fallback = _briefing_fallback(e, customer_id)
    card = next((c for c in fallback.get("briefing_cards", []) if c.get("id") == card_id), None) or {}
    basic = {
        "customer_id": customer_id, "card_id": card_id, "generated_by": "deterministic_fallback",
        "title": card.get("title") or "RM action",
        "answer": card.get("answer") or "Review the available evidence and record the next action in CRM.",
        "next_questions": ["What changed since the last interaction?", "Which document or fact is still missing?", "What follow-up should be recorded?"],
        "say_this": "Let me understand the current position clearly so we can take the right next step.",
        "crm_action": "Create RM note and follow-up task",
        "evidence": card.get("evidence", []),
    }
    if not llm.available():
        return basic
    evidence = {"selected_card": card, "rm_question": question, "customer_facts": e}
    schema = ('{"title":"...","answer":"...","next_questions":["..."],"say_this":"...",'
              '"what_not_to_say":"...","crm_action":"...","evidence":["..."]}')
    task = ("The RM clicked one briefing card. Generate a concise action drilldown: what it means, exactly what "
            "to ask next, what to say to the customer, what not to say, and the CRM action. Ground only in evidence.")
    try:
        out = llm.narrate_json(task, evidence, schema, temperature=0.45, max_tokens=900)
        out.update({"customer_id": customer_id, "card_id": card_id, "generated_by": "llm_grounded"})
        return out
    except Exception:
        return basic


def breach_income_copilot(store: DataStore, customer_id: str) -> dict:
    radar = breach_radar(store, customer_id)
    recon = income_reconciliation(store, customer_id)
    e = _facts(store, customer_id)
    h = radar.get("headroom", {})
    agg = recon.get("aggregate", {})
    findings = recon.get("findings", [])
    # deterministic scenario presets that make a demo obvious and clickable
    dp = h.get("drawing_power_inr") or h.get("sanction_limit_inr") or 0
    presets = [
        {"id": "buyer_delay", "label": "Buyer delay shock", "scenario": {"buyer_payment_delay_inr": round(dp * 0.10, 0), "delay_days": 45, "sales_drop_pct": 0, "additional_drawdown_inr": 0}},
        {"id": "sales_drop", "label": "GST sales softness", "scenario": {"buyer_payment_delay_inr": 0, "delay_days": 60, "sales_drop_pct": 20, "additional_drawdown_inr": 0}},
        {"id": "new_draw", "label": "New drawdown request", "scenario": {"buyer_payment_delay_inr": 0, "delay_days": 30, "sales_drop_pct": 0, "additional_drawdown_inr": round(dp * 0.12, 0)}},
    ]
    simulated = []
    for p in presets:
        try:
            sim = breach_simulate(store, customer_id, p["scenario"])
            simulated.append({**p, "projected": sim.get("projected"), "delta": sim.get("delta"), "recommended_actions": sim.get("recommended_actions", [])})
        except Exception:
            simulated.append(p)
    fallback = {
        "customer_id": customer_id, "generated_by": "deterministic_fallback",
        "control_tower_headline": f"Breach score {radar.get('breach_score')} with {agg.get('variance_pct')}% GST-bank variance.",
        "executive_read": "Use the radar and reconciliation together: headroom tells whether the account can absorb stress; income reconciliation tells whether turnover quality supports the review.",
        "rm_demo_script": [
            "Click the breach driver to explain why this customer is on watch.",
            "Run a buyer-delay stress scenario and show how utilisation/DP cover change.",
            "Open income reconciliation to show GST vs bank credits vs audited turnover.",
            "Convert the highest finding into a customer question and CRM follow-up.",
        ],
        "decision_questions": [
            "Is the facility heading toward a covenant or utilisation breach?",
            "Is reported income being routed through Contoso or sitting outside the account?",
            "What must the RM ask before a renewal/enhancement review?",
        ],
        "talk_tracks": [
            {"title": "Clarify utilisation pressure", "say": "Your account is running close to the working-capital line. Can we review the buyer payment cycle and upcoming collections?", "why": "Avoids alarming language and seeks operational facts."},
            {"title": "Close income variance", "say": "I want to reconcile GST sales with credits routed through the account so the review note is complete.", "why": "Clarification, not allegation."},
        ],
        "scenario_presets": simulated,
        "crm_actions": ["Create document request", "Create buyer-delay follow-up", "Prepare credit-review note", "Escalate service/document blocker"],
        "evidence_chips": ["utilization", "covenants", "stock_statements", "gst", "transactions", "financials"],
        "guardrail": "This is a pre-review diagnostic. It does not approve, reject, or accuse.",
    }
    if not llm.available():
        return fallback
    evidence = {
        "customer": {"name": e["customer"].get("display_name"), "risk": e["customer"].get("risk_category")},
        "facility": e["facility"], "conduct": e["conduct"],
        "breach_radar": radar,
        "income_reconciliation": {"aggregate": agg, "findings": findings, "months": recon.get("months", [])},
        "scenario_presets": simulated,
        "open_service_requests": e["service_requests"],
        "documents": e["documents"],
    }
    schema = (
        '{"control_tower_headline":"...","executive_read":"...",'
        '"rm_demo_script":["step-by-step demo actions"],'
        '"decision_questions":["questions the AI helps answer"],'
        '"talk_tracks":[{"title":"...","say":"customer-safe line","why":"reasoning"}],'
        '"crm_actions":["one-click actions"],"guardrail":"..."}'
    )
    task = (
        "Turn Breach Radar plus Income Reconciliation into a customer-demo-ready RM Assist control tower. "
        "Show why AI is valuable: it must connect breach trajectory, GST-bank-turnover variance, documents, and next RM questions. "
        "Write a step-by-step demo script, three decision questions, customer-safe talk tracks, and CRM actions. "
        "Use only evidence. Clarification-seeking language only; no fraud allegations; no approval promise."
    )
    try:
        ai = llm.narrate_json(task, evidence, schema, temperature=0.45, max_tokens=1600)
        return {**fallback, **ai, "generated_by": "llm_grounded", "scenario_presets": simulated,
                "evidence_chips": fallback["evidence_chips"]}
    except Exception:
        return fallback
