"""
backend/app/services/narrative.py

On-the-fly AI narratives (always live, grounded). Two products:

  playbook(customer)  — point 4: a full RM playbook for the day's conversation,
    sequencing the multiple engagement threads, framing each, and prepping the RM
    for likely customer moves. Grounded in conduct + EWS + threads + cross-sell.

  persona_narrative(customer, stakeholder) — point 5: how to position the
    relationship to ONE stakeholder, tuned to their role, priorities and concerns.

Both call Foundry gpt-4.1 via llm.narrate(), passing a structured evidence
object so the model narrates only from real data. If the model is unreachable,
both fall back to a deterministic summary so the demo never hard-fails.
"""
from __future__ import annotations

from app.store import DataStore
from app.services.analytics import AccountConduct, EWSEngine, EnhancementAssessor
from app.services.crosssell import opportunities
from app.services import llm


def _evidence_pack(store: DataStore, cid: str) -> dict:
    cust = store.one("customer_master", customer_id=cid) or {}
    prof = store.one("business_profile", customer_id=cid) or {}
    conduct = AccountConduct(store, cid).summary()
    ews = EWSEngine(store, cid).signals()
    enh = EnhancementAssessor(store, cid).assess()
    opps = opportunities(store, cid)
    threads = store.where("engagement_threads", customer_id=cid)
    interactions = store.where("interactions", customer_id=cid)
    last_int = interactions[-1] if interactions else None
    open_sr = [s for s in store.where("service_requests", customer_id=cid) if s.get("status") == "Open"]
    docs_pending = [d for d in store.where("documents", customer_id=cid)
                    if d.get("status") in ("Pending", "Expired") and d.get("required_flag") == "Y"]
    return {
        "customer": {"name": cust.get("display_name"), "constitution": cust.get("constitution"),
                     "segment": cust.get("segment"), "industry": prof.get("industry_description"),
                     "relationship_value_score": cust.get("relationship_value_score")},
        "conduct": {k: conduct.get(k) for k in
                    ("credits_trend_label", "credits_trend_pct", "avg_utilization_pct",
                     "peak_utilization_pct", "days_over_85_pct", "cash_intensity_pct",
                     "cheque_return_count", "top_counterparty", "top_counterparty_concentration_pct")},
        "ews_signals": [{"type": s["signal_type"], "severity": s["severity"],
                         "evidence": s["evidence_metric"], "guardrail": s["false_positive_guardrail"]}
                        for s in ews],
        "enhancement": {"eligible": enh["eligible_for_review"], "stance": enh["stance"],
                        "band_inr": enh.get("recommended_band_inr"), "caveats": enh.get("caveats")},
        "engagement_threads": [{"topic": t["topic"], "status": t["status"], "priority": t["priority"],
                                "angle": t["angle"]} for t in threads],
        "cross_sell_eligible": [o["product"] for o in opps if o["eligible"]],
        "cross_sell_blocked": [{"product": o["product"], "blocked_by": o["blocked_by"]}
                               for o in opps if not o["eligible"]],
        "last_interaction": ({"date": last_int["interaction_date"], "subject": last_int["subject"],
                              "summary": last_int["summary"], "sentiment": last_int.get("sentiment")}
                             if last_int else None),
        "open_service_tickets": [{"id": s["ticket_id"], "category": s["category"],
                                  "sentiment": s.get("customer_sentiment")} for s in open_sr],
        "documents_pending": sorted({d["document_type"] for d in docs_pending}),
    }


def playbook(store: DataStore, cid: str) -> dict:
    ev = _evidence_pack(store, cid)
    instruction = (
        "Produce the RM's playbook for today's conversation with this MSME customer. "
        "It must be specific to THIS customer's data and cover the multiple engagement "
        "threads (not just one). For every talking point and every recommended step, "
        "include the REASONING the RM needs — why it matters for this customer right now, "
        "tied to the evidence. Sequence the conversation sensibly (e.g. service recovery "
        "before any commercial ask). Never imply an approval; clarification not accusation."
    )
    schema = (
        '{ "opening_read": "2-3 sentence read of where the relationship stands", '
        '"conversation_sequence": [ {"order": 1, "thread": "topic", "what_to_do": "...", '
        '"reasoning": "why this, now, for this customer (cite evidence)"} ], '
        '"likely_pushback": [ {"pushback": "...", "response": "...", "reasoning": "..."} ], '
        '"critical_do_not": "the single most important do-not for this call", '
        '"do_not_reasoning": "why" }'
    )
    store.add_event("narrative.playbook_generated", {"customer_id": cid})
    try:
        data = llm.narrate_json(instruction, ev, schema, temperature=0.5, max_tokens=1600)
        return {"customer_id": cid, "mode": "ai", "structured": data, "evidence": ev}
    except Exception as e:
        return {"customer_id": cid, "mode": "fallback", "error": str(e),
                "structured": _fallback_playbook_struct(ev), "evidence": ev}


def persona_narrative(store: DataStore, cid: str, stakeholder_id: str) -> dict:
    stk = store.one("stakeholders", customer_id=cid, stakeholder_id=stakeholder_id)
    if not stk:
        return {"error": f"stakeholder {stakeholder_id} not found"}
    ev = _evidence_pack(store, cid)
    ev["target_stakeholder"] = {
        "name": stk["name"], "title": stk["title"], "priorities": stk["priorities"],
        "concerns": stk["concerns"], "influence": stk["influence"],
        "disposition": stk["disposition"], "decision_role": stk["decision_role"],
        "hooks": stk["hooks"],
    }
    instruction = (
        f"Produce guidance for how the RM should position the relationship specifically "
        f"to {stk['name']}, who is the {stk['title']} of this MSME. This person's "
        f"decision role is '{stk['decision_role']}' with {stk['influence']} influence and "
        f"a '{stk['disposition']}' disposition. EVERY talking point MUST carry a 'reasoning' "
        f"explaining why it lands with THIS specific persona given their role, priorities "
        f"and concerns — not generic advice. Tie reasoning to the customer's real evidence. "
        f"Stay within policy (recommendations not approvals; clarification not accusation)."
    )
    schema = (
        '{ "framing": "the overall angle for this persona and WHY it fits their role", '
        '"talking_points": [ {"point": "what to say (use real figures)", '
        '"reasoning": "why this resonates with THIS persona specifically — their role, '
        'priority or concern it addresses"} ], '
        '"likely_pushback": {"pushback": "what this persona will likely object to", '
        '"response": "how to respond", "reasoning": "why they would react this way"}, '
        '"the_ask": "the one next step to land with this persona", '
        '"ask_reasoning": "why this ask suits their decision role" }'
    )
    store.add_event("narrative.persona_generated", {"customer_id": cid, "stakeholder_id": stakeholder_id})
    try:
        data = llm.narrate_json(instruction, ev, schema, temperature=0.55, max_tokens=1400)
        return {"customer_id": cid, "stakeholder_id": stakeholder_id, "stakeholder": ev["target_stakeholder"],
                "mode": "ai", "structured": data}
    except Exception as e:
        return {"customer_id": cid, "stakeholder_id": stakeholder_id, "stakeholder": ev["target_stakeholder"],
                "mode": "fallback", "error": str(e), "structured": _fallback_persona_struct(stk, ev)}


def stakeholder_tree(store: DataStore, cid: str) -> dict:
    nodes = store.where("stakeholders", customer_id=cid)
    return {"customer_id": cid, "stakeholders": nodes}


# ---------- deterministic fallbacks (used only if Foundry is unreachable) ----------
def _fallback_playbook_struct(ev: dict) -> dict:
    seq = []
    for i, t in enumerate(ev["engagement_threads"], 1):
        seq.append({"order": i, "thread": t["topic"], "what_to_do": t["angle"],
                    "reasoning": f"Priority {t['priority']}, status {t['status']}."})
    return {
        "opening_read": f"{ev['customer']['name']}: credits {ev['conduct']['credits_trend_label']} "
                        f"({ev['conduct']['credits_trend_pct']}%), utilization {ev['conduct']['avg_utilization_pct']}%.",
        "conversation_sequence": seq,
        "likely_pushback": [{"pushback": "Wants a decision on the call",
                             "response": "Frame as review, not approval.",
                             "reasoning": "Credit decisions require appraisal and sign-off."}],
        "critical_do_not": "Do not commit any credit approval on the call.",
        "do_not_reasoning": f"Stance: {ev['enhancement']['stance']}.",
    }


def _fallback_persona_struct(stk: dict, ev: dict) -> dict:
    return {
        "framing": stk["hooks"],
        "talking_points": [
            {"point": f"Acknowledge priorities: {stk['priorities']}",
             "reasoning": f"As {stk['title']} ({stk['decision_role']}), these drive their decisions."},
            {"point": f"Address concerns: {stk['concerns']}",
             "reasoning": f"Their disposition is '{stk['disposition']}'."},
        ],
        "likely_pushback": {"pushback": stk["concerns"], "response": "Address with data and a plan.",
                            "reasoning": f"Top-of-mind for a {stk['title']}."},
        "the_ask": "Agree the next concrete step.",
        "ask_reasoning": f"Matches their decision role: {stk['decision_role']}.",
    }
