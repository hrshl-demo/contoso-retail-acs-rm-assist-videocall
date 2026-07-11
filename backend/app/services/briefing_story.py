"""Progressive AI stories for the non-rescue RM Assist experiences.

The previous POC rendered relationship thesis, briefing cards, risk, opportunity,
blockers and actions together. This module turns those outputs into a cumulative,
stage-gated story. Each stage answers one decision question, exposes only the
new evidence needed for that question, and passes a compact conclusion to the
next stage.

Observed facts and deterministic policy decisions remain separate from AI prose.
The LLM may explain, prioritise and phrase; it cannot invent figures, approve
credit, or override service/risk guardrails.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from app.store import DataStore
from app.config import get_settings
from app.services import llm
from app.services.analytics import AccountConduct, EWSEngine, EnhancementAssessor
from app.services.crosssell import opportunities
from app.services.portfolio import priority_queue
from app.services.daily_planner import build_dilo
from app.services.relationship import recent_transactions, crm_timeline, _top_counterparties
from app.services.relationship_decisioning import build_decision_pack
from app.services.search import retrieve as retrieve_policy_chunks


CUSTOMER_STAGES = [
    {
        "stage": 1, "id": "relationship-baseline", "short": "Baseline",
        "title": "Establish the relationship baseline",
        "question": "Who is this customer, and what is structurally important before we interpret any signal?",
        "capability": "Entity and relationship synthesis",
    },
    {
        "stage": 2, "id": "change-detection", "short": "What changed",
        "title": "Detect what changed and why today matters",
        "question": "Which recent change makes this relationship actionable now?",
        "capability": "Temporal change detection",
    },
    {
        "stage": 3, "id": "posture-resolution", "short": "Posture",
        "title": "Resolve the relationship posture",
        "question": "Should the RM grow, protect, stabilise or simply watch this relationship?",
        "capability": "Competing-lane prioritisation",
    },
    {
        "stage": 4, "id": "conversation-frame", "short": "Conversation",
        "title": "Frame the customer conversation",
        "question": "How should the RM open, what should be asked, and what must not be promised?",
        "capability": "Customer-safe language and question planning",
    },
    {
        "stage": 5, "id": "day-commitment", "short": "Commit",
        "title": "Commit the day plan",
        "question": "What is the single outcome for today, and which CRM actions create follow-through?",
        "capability": "Action sequencing and human-in-the-loop handoff",
    },
]

PORTFOLIO_STAGES = [
    {
        "stage": 1, "id": "portfolio-scan", "short": "Scan",
        "title": "Scan the portfolio",
        "question": "Where does the RM's attention need to go first?",
        "capability": "Portfolio triage",
    },
    {
        "stage": 2, "id": "priority-explanation", "short": "Explain",
        "title": "Explain the top priorities",
        "question": "Why are these customers ahead of the rest today?",
        "capability": "Evidence-backed ranking explanation",
    },
    {
        "stage": 3, "id": "day-sequencing", "short": "Sequence",
        "title": "Sequence the working day",
        "question": "What order minimises risk, protects SLAs and preserves growth time?",
        "capability": "Constraint-aware day orchestration",
    },
    {
        "stage": 4, "id": "action-commitment", "short": "Commit",
        "title": "Commit the operating plan",
        "question": "Which calls, documents and CRM tasks should be committed now?",
        "capability": "Operational action planning",
    },
]

_CACHE: dict[str, dict] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _inr(v: Any) -> str:
    n = _num(v)
    if abs(n) >= 1e7:
        return f"₹{n/1e7:.2f} Cr"
    if abs(n) >= 1e5:
        return f"₹{n/1e5:.1f} L"
    return f"₹{n:,.0f}"


def _safe_date(v: Any) -> str:
    return str(v or "")[:10]


def _cache_key(kind: str, entity_id: str, stage: int, evidence: dict) -> str:
    blob = json.dumps(evidence, sort_keys=True, default=str, separators=(",", ":"))
    fp = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    return f"{kind}:{entity_id}:{stage}:{fp}"


def _source(label: str, value: Any, source: str, tone: str = "neutral", detail: str = "") -> dict:
    return {"label": label, "value": value, "source": source, "tone": tone, "detail": detail}


def _customer_facts(store: DataStore, customer_id: str) -> dict:
    cust = store.one("customer_master", customer_id=customer_id) or {}
    profile = store.one("business_profile", customer_id=customer_id) or {}
    facility = store.one("facilities", customer_id=customer_id) or {}
    conduct = AccountConduct(store, customer_id).summary()
    ews = EWSEngine(store, customer_id).signals()
    enhancement = EnhancementAssessor(store, customer_id).assess()
    opps = opportunities(store, customer_id)
    docs = [d for d in store.where("documents", customer_id=customer_id)
            if d.get("status") in ("Pending", "Expired", "Overdue") or d.get("blocking_flag") == "Y"]
    service = [s for s in store.where("service_requests", customer_id=customer_id)
               if str(s.get("status", "")).lower() == "open"]
    interactions = store.where("interactions", customer_id=customer_id)
    tasks = [t for t in store.where("tasks", customer_id=customer_id)
             if str(t.get("status", "")).lower() in ("open", "pending", "")]
    timeline = crm_timeline(store, customer_id).get("events", [])
    return {
        "customer": cust,
        "profile": profile,
        "facility": facility,
        "conduct": conduct,
        "ews": ews,
        "enhancement": enhancement,
        "opportunities": opps,
        "documents": docs,
        "service_requests": service,
        "interactions": interactions,
        "tasks": tasks,
        "timeline": timeline,
        "recent_transactions": recent_transactions(store, customer_id, 10),
        "top_counterparties": _top_counterparties(store, customer_id, 5),
        "decision_pack": build_decision_pack(store, customer_id),
    }


def _posture(f: dict) -> tuple[str, str, list[dict]]:
    ews = f["ews"]
    service = f["service_requests"]
    docs = f["documents"]
    enh = f["enhancement"]
    eligible = [o for o in f["opportunities"] if o.get("eligible")]
    critical = [x for x in ews if x.get("severity") == "Critical"]
    high = [x for x in ews if x.get("severity") == "High"]
    negative_service = [s for s in service if str(s.get("customer_sentiment", "")).lower() in ("negative", "concerned")]

    lanes = [
        {"lane": "Stabilise", "active": bool(critical or len(high) >= 2),
         "reason": "Material risk signals require clarification or remediation before commercial action."},
        {"lane": "Protect", "active": bool(service or negative_service),
         "reason": "An open service issue or customer dissatisfaction should be resolved before selling."},
        {"lane": "Grow", "active": bool(eligible) and not bool(critical or high or service),
         "reason": "Eligible products exist and no service or material risk issue dominates the relationship."},
        {"lane": "Watch", "active": not bool(critical or high or service or eligible),
         "reason": "No immediate risk, service or growth trigger dominates today."},
    ]
    if critical or len(high) >= 2:
        return "Stabilise", lanes[0]["reason"], lanes
    if service:
        return "Protect", lanes[1]["reason"], lanes
    if eligible and not any(d.get("blocking_flag") == "Y" for d in docs):
        return "Grow", lanes[2]["reason"], lanes
    if eligible:
        return "Watch", "A growth opportunity exists, but blockers or unresolved facts prevent an immediate pitch.", lanes
    return "Watch", lanes[3]["reason"], lanes


def _last_interaction(f: dict) -> dict:
    rows = f.get("interactions") or []
    return sorted(rows, key=lambda x: str(x.get("interaction_date", "")))[-1] if rows else {}


def _customer_stage_fallback(f: dict, stage: int) -> dict:
    c, p, fac, conduct = f["customer"], f["profile"], f["facility"], f["conduct"]
    ews, docs, service = f["ews"], f["documents"], f["service_requests"]
    eligible = [o for o in f["opportunities"] if o.get("eligible")]
    last = _last_interaction(f)
    posture, posture_reason, lanes = _posture(f)
    name = c.get("display_name") or "Customer"
    stage_meta = CUSTOMER_STAGES[stage - 1]
    pack = f.get("decision_pack") or {}
    calculations = pack.get("calculations") or []
    policies = pack.get("policy_matches") or []
    reasoning = pack.get("reasoning_summary") or []
    contradictions = pack.get("contradictions") or []
    solutions = pack.get("solution_options") or []
    recommended = pack.get("recommended_solution") or {}
    metrics = pack.get("decision_metrics") or {}
    runtime = pack.get("runtime_contract") or {}

    base = {
        "stage": stage,
        "stage_id": stage_meta["id"],
        "title": stage_meta["title"],
        "question": stage_meta["question"],
        "capability": stage_meta["capability"],
        "customer_id": c.get("customer_id"),
        "display_name": name,
        "generated_by": "deterministic_fallback",
        "generated_at": _now(),
        "human_review_required": True,
        "confidence": 0.88,
        "uncertainty": "Only unresolved facts that can change the recommended solution require RM or customer validation.",
        "observed_facts": [],
        "new_findings": [],
        "evidence": [],
        "withheld_until_later": [],
        "next_gate": {},
        "decision": {},
        "conversation": {},
        "actions": [],
        "crm_candidates": [],
        "calculations": [],
        "policy_matches": [],
        "reasoning_summary": [],
        "contradictions": [],
        "solution_options": [],
        "recommended_solution": {},
        "quantified_outcomes": [],
        "guardrail": "AI performs runtime synthesis over validated customer calculations and SOP clauses; authorised humans retain all service, pricing and credit decisions.",
        "runtime_contract": runtime,
        "execution": {
            "runtime_generated": True,
            "llm_invoked": False,
            "mode": "Runtime deterministic safety engine",
            "model": "rule-and-calculation-engine",
            "prompt_version": runtime.get("prompt_version"),
            "calculation_version": runtime.get("calculation_version"),
            "policy_index_version": runtime.get("policy_index_version"),
            "calculations_executed": runtime.get("calculations_executed", len(calculations)),
            "sop_clauses_retrieved": runtime.get("sop_clauses_retrieved", len(policies)),
            "evidence_fingerprint": runtime.get("evidence_fingerprint"),
            "cache_hit": False,
            "rag_invoked": False,
            "sop_retrieval_mode": "Local deterministic SOP decision index",
            "rag_grounded": False,
            "rag_chunks_retrieved": 0,
        },
        "retrieved_sop_chunks": [],
        "data_freshness": {
            "customer_updated_at": c.get("updated_at") or "",
            "last_interaction_date": last.get("interaction_date") or "",
            "latest_transaction_date": max([str(t.get("txn_date", "")) for t in f["recent_transactions"]] or [""]),
            "analysis_as_of": pack.get("as_of") or "",
        },
    }

    if stage == 1:
        baseline_calcs = [x for x in calculations if x.get("calculation_id") in (
            "CALC-TURNOVER", "CALC-CAPTURE", "CALC-CARD-UTIL", "CALC-DEBT-SERVICE")][:4]
        facts = [
            _source("Segment", c.get("segment") or "—", "customer_master"),
            _source("Relationship since", c.get("customer_since") or "—", "customer_master"),
            _source("Risk category", c.get("risk_category") or "—", "customer_master"),
            _source("Primary facility", fac.get("facility_type") or "—", "loan_facilities"),
            _source("Facility limit", _inr(fac.get("sanction_limit_inr")), "loan_facilities"),
            _source("Average utilisation", f"{conduct.get('avg_utilization_pct', 0)}%", "account_conduct"),
        ]
        base.update({
            "headline": f"{name}: establish the financial baseline before interpreting any change.",
            "narrative": (f"The engine assembled the relationship, facility, transaction, bureau and CRM history as of {pack.get('as_of') or 'the latest record'}. "
                          "No sales or remediation decision is made in this chapter; it establishes the denominators used in later calculations."),
            "observed_facts": facts,
            "calculations": baseline_calcs,
            "new_findings": [
                "The customer baseline has been normalised across core banking, credit, CRM and bureau sources.",
                f"{len(calculations)} quantitative tests and {len(policies)} potential SOP clauses are available for later-stage decisioning.",
            ],
            "evidence": ["customer_master", "business_profile", "loan_facilities", "transactions", "bureau_summary", "crm_timeline"],
            "withheld_until_later": ["No posture yet", "No solution recommendation yet", "No customer script yet"],
            "next_gate": {"label": "Diagnose the change", "reason": "The next chapter quantifies what moved and tests whether the change is operational, financial or merely timing-related."},
        })
    elif stage == 2:
        top_signal = ews[0] if ews else {}
        facts = [
            _source("Credit trend", f"{conduct.get('credits_trend_label','—')} {conduct.get('credits_trend_pct',0)}%", "account_conduct",
                    "risk" if _num(conduct.get("credits_trend_pct")) < 0 else "positive"),
            _source("Open service cases", len(service), "service_requests", "risk" if service else "positive"),
            _source("Oldest SLA breach", f"{metrics.get('max_sla_overdue_days',0)} days", "service_requests", "risk" if metrics.get("max_sla_overdue_days") else "positive"),
            _source("Debt-service load", f"{metrics.get('debt_service_to_credits_pct',0):.1f}%", "calculation_engine",
                    "risk" if metrics.get("debt_service_to_credits_pct",0) > 35 else "neutral"),
            _source("Highest warning", (f"{top_signal.get('signal_type')} · {top_signal.get('severity')}" if top_signal else "None"), "ews_engine",
                    "risk" if top_signal else "positive"),
        ]
        base.update({
            "headline": (f"The actionable change is not one signal: {len(calculations)} calculations connect income, debt, service and conduct."),
            "narrative": (f"AI calculated the size of the change, reconciled it with declared income and tested whether open service events distort the risk picture. "
                          f"It retrieved {len(policies)} applicable SOP clauses before moving to a relationship posture."),
            "observed_facts": facts,
            "calculations": calculations,
            "policy_matches": policies,
            "reasoning_summary": reasoning,
            "contradictions": contradictions,
            "new_findings": [x.get("interpretation") for x in calculations[:5] if x.get("interpretation")],
            "evidence": ["transactions", "account_conduct", "loan_facilities", "repayment_history", "bureau_summary", "service_requests", "SOP decision index"],
            "uncertainty": ("The remaining uncertainty is limited to facts that change the remedy—such as dispute eligibility, verified hardship or actual product-system pricing—not a generic request to 'clarify the customer'."),
            "withheld_until_later": ["The recommended intervention is shown only after policy and solution lanes are compared."],
            "next_gate": {"label": "Resolve posture and solution", "reason": "Quantified facts now need to be converted into a permitted bank intervention, not another list of tasks."},
        })
    elif stage == 3:
        facts = [
            _source("Risk signals", len(ews), "ews_engine", "risk" if ews else "positive"),
            _source("Applicable SOP clauses", len(policies), "SOP decision index", "neutral"),
            _source("Solution lanes tested", len(solutions), "solution_playbooks", "neutral"),
            _source("Eligible conventional products", len(eligible), "product_rules", "positive" if eligible else "neutral"),
        ]
        chosen_title = recommended.get("title") or posture
        base.update({
            "headline": f"Resolved posture: {posture}. Recommended intervention: {chosen_title}.",
            "narrative": (recommended.get("description") or posture_reason),
            "observed_facts": facts,
            "policy_matches": policies,
            "reasoning_summary": reasoning,
            "contradictions": contradictions,
            "solution_options": solutions,
            "recommended_solution": recommended,
            "quantified_outcomes": recommended.get("quantified_outcomes", []),
            "new_findings": [
                f"{posture} is the dominant RM posture because it best satisfies the applicable SOP constraints.",
                f"{chosen_title} is selected from {len(solutions)} tested intervention lane(s).",
                f"{len(pack.get('suppressed_products') or [])} product(s) are suppressed because they conflict with the resolved posture or eligibility rules.",
            ],
            "evidence": ["calculation_engine", "SOP clauses", "product_rules", "service_requests", "bureau_summary"],
            "decision": {
                "posture": posture,
                "reason": posture_reason,
                "candidate_lanes": lanes,
                "recommended_solution_id": recommended.get("solution_id"),
                "recommended_solution_title": chosen_title,
                "suppressed_products": [x.get("product") for x in pack.get("suppressed_products", []) if x.get("product")],
                "eligible_products": [o.get("product") for o in eligible][:4],
            },
            "withheld_until_later": ["Customer wording and CRM writeback follow only after the solution is fixed."],
            "next_gate": {"label": "Translate solution for the customer", "reason": "The RM now needs an accurate explanation of the bank's proposed remedy, conditions and expected impact."},
        })
    elif stage == 4:
        rec_title = recommended.get("title") or posture
        outcomes = recommended.get("quantified_outcomes") or []
        if posture in ("Stabilise", "Protect"):
            opening = (f"I have reviewed the account in two parts rather than treating every issue as one problem. "
                       f"The bank's recommended next step is {rec_title.lower()}; I will explain what can be decided now, what needs authorised review and the expected financial effect.")
            questions = [
                "Please confirm only the fact that changes the proposed remedy or eligibility.",
                "Does the quantified payment or service-recovery scenario match your current affordability and objective?",
                "May I record your consent to submit the selected solution for authorised review?",
            ]
            avoid = "Do not promise chargeback, waiver, pricing, restructuring or approval; explain the calculated scenario and the authorised decision path."
        elif posture == "Grow":
            opening = (f"Your current conduct supports a controlled review for {rec_title.lower()}. "
                       "Before I initiate it, I want to validate the goal and compare the proposed option with your existing products and doing nothing.")
            questions = ["What outcome are you trying to achieve, and by when?", "What amount or product feature is genuinely required?", "May I run the current eligibility and suitability check?" ]
            avoid = "Do not imply that a rate, amount, limit or sanction has already been approved."
        else:
            opening = "The current evidence does not justify a sales or remediation action. I want to validate the one material uncertainty and agree when the relationship should next be reviewed."
            questions = ["What material fact has changed since the last review?", "Is there a time-bound need or service issue not visible in our records?", "May I update the record and set the next evidence-based review?" ]
            avoid = "Do not manufacture urgency or a product need."
        base.update({
            "headline": f"Explain the solution—not the internal task list: {rec_title}.",
            "narrative": opening,
            "observed_facts": [
                _source("Resolved posture", posture, "stage_3_decision", "positive" if posture == "Grow" else "risk" if posture in ("Stabilise", "Protect") else "neutral"),
                _source("Recommended solution", rec_title, "solution_playbooks"),
                _source("Quantified outcomes", len(outcomes), "calculation_engine"),
                _source("Applicable SOP clauses", len(policies), "SOP decision index"),
            ],
            "policy_matches": policies,
            "recommended_solution": recommended,
            "quantified_outcomes": outcomes,
            "new_findings": ["The customer conversation is anchored to a calculated remedy, conditions and measurable impact—not a generic request for documents."],
            "evidence": ["stage_3_decision", "recommended_solution", "calculation_engine", "SOP guardrails"],
            "conversation": {
                "opening_line": opening,
                "questions": questions,
                "do_not_say": avoid,
                "sequence": ["State the separated diagnosis", "Explain the calculated remedy", "Validate only decisive facts", "Seek consent for authorised review"],
                "solution_title": rec_title,
                "customer_outcomes": outcomes,
            },
            "next_gate": {"label": "Commit the resolution package", "reason": "The final chapter converts the chosen remedy into authorised decisions, owners and measurable outcomes."},
        })
    else:
        steps = recommended.get("steps") or []
        action_list = []
        for i, step in enumerate(steps, 1):
            action_list.append({
                "order": step.get("order", i),
                "action": step.get("action"),
                "owner": step.get("owner") or "RM",
                "why": "; ".join(step.get("evidence") or recommended.get("policy_refs") or []),
                "due": step.get("due") or "As per workflow",
                "decision_type": "authorised bank decision" if any(w in str(step.get("action", "")).lower() for w in ("approve", "eligibility", "review", "generate")) else "operational step",
            })
        crm = []
        for step in action_list:
            crm.append({
                "type": "decision_task" if step.get("decision_type") == "authorised bank decision" else "task",
                "title": step.get("action"),
                "approval_required": True,
                "owner": step.get("owner"),
            })
        base.update({
            "headline": f"Today's outcome is a bank resolution package: {recommended.get('title') or posture}.",
            "narrative": (recommended.get("description") or "A quantified, policy-permitted resolution package is ready for human approval."),
            "observed_facts": [
                _source("Final posture", posture, "stage_3_decision"),
                _source("Recommended solution", recommended.get("title") or "—", "solution_playbooks"),
                _source("Authorised decisions required", len(action_list), "workflow_engine"),
                _source("Quantified outcomes", len(recommended.get("quantified_outcomes") or []), "calculation_engine"),
            ],
            "policy_matches": policies,
            "reasoning_summary": reasoning,
            "recommended_solution": recommended,
            "quantified_outcomes": recommended.get("quantified_outcomes", []),
            "solution_options": solutions,
            "new_findings": [
                "The RM is not being told merely to own or clarify a case; the workflow specifies the resolution decision, quantitative effect, policy basis, owner and approval gate.",
                "KYC or document work is included only when it changes eligibility or execution of the chosen remedy.",
            ],
            "evidence": ["recommended_solution", "SOP clauses", "calculation_engine", "authorisation matrix"],
            "actions": action_list,
            "crm_candidates": crm,
            "uncertainty": recommended.get("guardrail") or "Authorised humans must validate the final outcome and system-generated terms.",
            "next_gate": {"label": "Continue to stakeholder map", "reason": "The thesis has reached a decision-ready resolution package."},
        })
    return base

def _retrieve_runtime_sop_evidence(fallback: dict) -> dict:
    """Retrieve live SOP chunks when Azure AI Search is available.

    The deterministic policy matches remain the decision guardrail. Live RAG is
    used to corroborate those matches and make the runtime path visible in the
    POC. If Search is unavailable, the caller keeps the validated local policy
    index and explicitly reports that fallback.
    """
    policies = fallback.get("policy_matches") or []
    recommended = fallback.get("recommended_solution") or {}
    calculations = fallback.get("calculations") or []
    query_parts = [
        str(recommended.get("title") or fallback.get("headline") or "retail relationship decision"),
        " ".join(str(p.get("title") or "") for p in policies[:5]),
        " ".join(str(p.get("sop_ref") or "") for p in policies[:5]),
        " ".join(str(c.get("label") or "") for c in calculations[:6]),
        "bank SOP decision eligibility service recovery restructuring dispute KYC suitability",
    ]
    query = " | ".join(x.strip() for x in query_parts if x and x.strip())[:1800]
    try:
        result = retrieve_policy_chunks(query, top_k=5)
    except Exception as exc:  # retrieval must never break the relationship journey
        return {
            "grounded": False, "query": query, "results": [],
            "reason": exc.__class__.__name__,
            "mode": "Local deterministic SOP decision index",
        }
    hits = []
    for row in (result.get("results") or [])[:5]:
        hits.append({
            "chunk_id": row.get("chunk_id"),
            "sop_id": row.get("sop_id"),
            "sop_title": row.get("sop_title"),
            "section_title": row.get("section_title"),
            "content": str(row.get("content") or "")[:900],
            "score": row.get("reranker_score") if row.get("reranker_score") is not None else row.get("score"),
        })
    grounded = bool(result.get("grounded") and hits)
    return {
        "grounded": grounded,
        "query": query,
        "results": hits,
        "reason": result.get("reason") or ("" if grounded else "No live policy chunk met the retrieval criteria"),
        "mode": "Live policy retrieval (hybrid + semantic)" if grounded else "Local deterministic SOP decision index",
    }


def _ai_enrich_customer(fallback: dict, facts: dict) -> dict:
    if not llm.available():
        return fallback
    stage = fallback["stage"]
    live_sop = _retrieve_runtime_sop_evidence(fallback)
    evidence = {
        "stage": {k: fallback[k] for k in ("stage", "stage_id", "title", "question", "capability")},
        "observed_facts": fallback.get("observed_facts", []),
        "calculations": fallback.get("calculations", []),
        "policy_matches": fallback.get("policy_matches", []),
        "reasoning_summary": fallback.get("reasoning_summary", []),
        "contradictions": fallback.get("contradictions", []),
        "deterministic_decision": fallback.get("decision", {}),
        "recommended_solution": fallback.get("recommended_solution", {}),
        "solution_options": fallback.get("solution_options", []),
        "quantified_outcomes": fallback.get("quantified_outcomes", []),
        "conversation_constraints": fallback.get("conversation", {}),
        "actions": fallback.get("actions", []),
        "runtime_sop_retrieval": live_sop,
        "guardrail": fallback["guardrail"],
    }
    schema = (
        '{"headline":"one decisive, quantitative headline",'
        '"narrative":"3-5 sentences connecting customer facts, calculations, SOP tests and the bank solution",'
        '"confidence":0.0,'
        '"uncertainty":"only the unresolved fact that can materially change the solution",'
        '"new_findings":["non-duplicative stage findings"],'
        '"policy_reasoning_summary":["fact -> SOP clause -> decision implication"],'
        '"solution_rationale":"why the recommended intervention solves the measured problem better than alternatives",'
        '"calculation_callouts":["important numerical conclusion with formula/result"]}'
    )
    task = (
        f"Generate chapter {stage} of a progressive RM relationship-decision story at runtime. "
        "This must demonstrate intelligence, not summarisation. Explicitly connect the supplied calculations to the "
        "validated policy matches and any live Azure AI Search SOP chunks, then to a permitted bank solution. Cite live "
        "chunk IDs or SOP titles when they are present. Use major numerical figures and explain why they "
        "change the decision. Never replace the validated calculations, policy rules, posture, recommended solution or "
        "actions. Do not tell the RM merely to own, clarify or collect documents; explain the actual resolution, product "
        "or service intervention already present in the evidence. If a document is relevant, state exactly which decision "
        "it unlocks. Avoid repeating labels from the cards. Return only the requested JSON."
    )
    try:
        ai = llm.narrate_json(task, evidence, schema, temperature=0.25, max_tokens=1100)
        out = deepcopy(fallback)
        out["headline"] = str(ai.get("headline") or fallback["headline"])
        out["narrative"] = str(ai.get("narrative") or fallback["narrative"])
        out["confidence"] = max(0.0, min(1.0, _num(ai.get("confidence"), fallback["confidence"])))
        out["uncertainty"] = str(ai.get("uncertainty") or fallback["uncertainty"])
        if isinstance(ai.get("new_findings"), list) and ai["new_findings"]:
            out["new_findings"] = [str(x) for x in ai["new_findings"][:6]]
        out["ai_synthesis"] = {
            "policy_reasoning_summary": [str(x) for x in (ai.get("policy_reasoning_summary") or [])[:6]],
            "solution_rationale": str(ai.get("solution_rationale") or ""),
            "calculation_callouts": [str(x) for x in (ai.get("calculation_callouts") or [])[:6]],
        }
        out["generated_by"] = "llm_grounded"
        out["generated_at"] = _now()
        out["retrieved_sop_chunks"] = live_sop.get("results", [])
        execution = dict(out.get("execution") or {})
        execution.update({
            "llm_invoked": True,
            "mode": "Runtime AI synthesis",
            "model": "Contoso AI engine",
            "cache_hit": False,
            "structured_response": True,
            "rag_invoked": True,
            "sop_retrieval_mode": live_sop.get("mode"),
            "rag_grounded": bool(live_sop.get("grounded")),
            "rag_chunks_retrieved": len(live_sop.get("results") or []),
            "rag_query": live_sop.get("query"),
        })
        out["execution"] = execution
        return out
    except Exception as exc:
        out = deepcopy(fallback)
        out["retrieved_sop_chunks"] = live_sop.get("results", [])
        execution = dict(out.get("execution") or {})
        execution.update({
            "fallback_reason": exc.__class__.__name__, "cache_hit": False,
            "rag_invoked": True,
            "sop_retrieval_mode": live_sop.get("mode"),
            "rag_grounded": bool(live_sop.get("grounded")),
            "rag_chunks_retrieved": len(live_sop.get("results") or []),
        })
        out["execution"] = execution
        return out

def customer_story_stage(store: DataStore, customer_id: str, stage: int = 1, force: bool = False) -> dict:
    if stage < 1 or stage > len(CUSTOMER_STAGES):
        raise ValueError(f"stage must be between 1 and {len(CUSTOMER_STAGES)}")
    facts = _customer_facts(store, customer_id)
    if not facts["customer"]:
        raise KeyError(customer_id)
    fallback = _customer_stage_fallback(facts, stage)
    evidence_for_key = {
        "customer": facts["customer"], "facility": facts["facility"], "conduct": facts["conduct"],
        "ews": facts["ews"], "documents": facts["documents"], "service": facts["service_requests"],
        "opportunities": facts["opportunities"], "interactions": facts["interactions"][-3:], "stage": stage,
        "decision_fingerprint": (facts.get("decision_pack") or {}).get("runtime_contract", {}).get("evidence_fingerprint"),
    }
    key = _cache_key("customer", customer_id, stage, evidence_for_key)
    if not force and key in _CACHE:
        cached = deepcopy(_CACHE[key])
        if isinstance(cached.get("execution"), dict):
            cached["execution"]["cache_hit"] = True
        return cached
    result = _ai_enrich_customer(fallback, facts)
    result["analysis_id"] = f"REL-{customer_id}-{stage}-{key.rsplit(':',1)[-1][:8]}"
    result["stage_count"] = len(CUSTOMER_STAGES)
    result["stages"] = CUSTOMER_STAGES
    _CACHE[key] = deepcopy(result)
    store.add_event("ai.relationship_story_stage", {"customer_id": customer_id, "stage": stage, "generated_by": result["generated_by"]})
    return result


def customer_story_status(store: DataStore, customer_id: str) -> dict:
    return {
        "customer_id": customer_id,
        "stages": CUSTOMER_STAGES,
        "stage_count": len(CUSTOMER_STAGES),
        "ai_available": llm.available(),
        "architecture": "progressive-stage-gated",
        "principles": ["one decision question per stage", "observed facts separated from inference", "human approval for CRM writes"],
    }



def _portfolio_solution_summary(store: DataStore, customer_id: str) -> dict:
    """Return the actual bank intervention for a portfolio customer.

    Rescue cases use their validated rescue plan. Conventional customers use the
    quantitative/SOP decision pack. This prevents the Daily Briefing from ending
    in generic tasks such as "own the case" or "clarify with customer".
    """
    rescue = getattr(store, "rescue_cases_by_customer", {}).get(customer_id)
    if rescue:
        plan = rescue.get("rescuePlan") or []
        verdict = rescue.get("finalVerdict") or {}
        title = verdict.get("title") or verdict.get("headline") or rescue.get("caseSummary", {}).get("primaryConcern") or "Customer rescue intervention"
        return {
            "title": title,
            "description": verdict.get("summary") or verdict.get("description") or (plan[0].get("reason") if plan else "Run the authorised rescue workflow."),
            "steps": [{
                "action": x.get("action"), "owner": x.get("ownerTeam"), "due": x.get("dueDate"),
                "reason": x.get("reason"),
            } for x in plan[:3]],
            "impact": verdict.get("customerOutcome") or verdict.get("bankOutcome") or (plan[0].get("reason") if plan else "Risk contained and customer protected"),
            "policy_refs": [p.get("policyId") or p.get("policyName") for p in (rescue.get("policies") or [])[:3]],
            "source": "validated_rescue_case",
        }
    pack = build_decision_pack(store, customer_id)
    rec = pack.get("recommended_solution") or {}
    return {
        "title": rec.get("title") or "Evidence-led relationship review",
        "description": rec.get("description") or "Use the quantified relationship decision pack.",
        "steps": [{
            "action": x.get("action"), "owner": x.get("owner"), "due": x.get("due"),
            "reason": "; ".join(x.get("evidence") or []),
        } for x in (rec.get("steps") or [])[:3]],
        "impact": ((rec.get("quantified_outcomes") or [{}])[0].get("value") or rec.get("guardrail") or "Decision-ready intervention"),
        "outcomes": rec.get("quantified_outcomes") or [],
        "policy_refs": rec.get("policy_refs") or [],
        "source": "quantitative_sop_decision_pack",
        "evidence_fingerprint": (pack.get("runtime_contract") or {}).get("evidence_fingerprint"),
    }

def _portfolio_counts(queue: list[dict]) -> dict:
    counts = {"Customer Intervention": 0, "Risk Watch": 0, "Growth": 0, "Renewal Due": 0}
    for row in queue:
        counts[row.get("bucket", "Renewal Due")] = counts.get(row.get("bucket", "Renewal Due"), 0) + 1
    return counts


def _portfolio_fallback(store: DataStore, rm_id: str, stage: int, focus_customer_id: str | None = None) -> dict:
    meta = PORTFOLIO_STAGES[stage - 1]
    # The synthetic portfolio uses mixed historical RM identifiers; the dashboard
    # represents one consolidated demo book, so triage the full assigned queue.
    queue = priority_queue(store)
    counts = _portfolio_counts(queue)
    focus = next((x for x in queue if x.get("customer_id") == focus_customer_id), None) or (queue[0] if queue else {})
    plan = build_dilo(store, rm_id)
    base = {
        "stage": stage, "stage_id": meta["id"], "title": meta["title"], "question": meta["question"],
        "capability": meta["capability"], "rm_id": rm_id, "generated_by": "deterministic_fallback",
        "generated_at": _now(), "human_review_required": True, "confidence": 0.86,
        "uncertainty": "The RM may re-order the day when new customer or SLA information arrives.",
        "portfolio_counts": counts, "focus_customer_id": focus.get("customer_id"),
        "queue": [], "observed_facts": [], "new_findings": [], "day_sequence": [], "actions": [],
        "evidence": [], "next_gate": {},
        "guardrail": "This is a suggested operating plan. It does not contact customers or write to CRM automatically.",
        "execution": {
            "runtime_generated": True,
            "llm_invoked": False,
            "mode": "Runtime portfolio decision engine",
            "model": "rule-and-calculation-engine",
            "prompt_version": "portfolio-decision-v2.1",
            "calculations_executed": 0,
            "sop_clauses_retrieved": 0,
            "rag_chunks_retrieved": 0,
            "sop_retrieval_mode": "Customer-level validated decision packs",
            "cache_hit": False,
            "evidence_fingerprint": "portfolio-runtime",
        },
    }
    if stage == 1:
        top = queue[:5]
        base.update({
            "headline": f"{len(queue)} relationships scanned; {counts.get('Customer Intervention',0)+counts.get('Risk Watch',0)} require intervention or risk attention first.",
            "narrative": "AI has only triaged the book at this stage. It has not yet generated customer talk tracks or product actions.",
            "queue": [{**x, "rank": i+1} for i, x in enumerate(top)],
            "observed_facts": [
                _source("Portfolio relationships", len(queue), "portfolio_assignments"),
                _source("Customer interventions", counts.get("Customer Intervention", 0), "priority_queue", "risk"),
                _source("Risk watch", counts.get("Risk Watch", 0), "priority_queue", "risk"),
                _source("Growth", counts.get("Growth", 0), "priority_queue", "positive"),
            ],
            "new_findings": ["The queue is reduced to a ranked attention list with one reason per customer."],
            "evidence": ["portfolio_assignments", "priority_queue", "relationship_value_score"],
            "next_gate": {"label": "Explain priorities", "reason": "Ranking is not useful until the RM can see why the top customers outrank the rest."},
        })
    elif stage == 2:
        top = queue[:4]
        matrix = []
        for i, x in enumerate(top):
            sol = _portfolio_solution_summary(store, x.get("customer_id"))
            matrix.append({
                "rank": i+1, "customer_id": x.get("customer_id"), "customer": x.get("display_name"),
                "bucket": x.get("bucket"), "why_now": x.get("reason"),
                "critical_signals": x.get("critical_signals", 0), "high_signals": x.get("high_signals", 0),
                "document_blockers": x.get("blocking_documents", 0), "relationship_value_score": x.get("relationship_value_score", 0),
                "recommended_action": sol.get("title"), "solution_impact": sol.get("impact"),
                "policy_refs": sol.get("policy_refs", []), "solution_source": sol.get("source"),
            })
        customer_packs = [build_decision_pack(store, x.get("customer_id")) for x in top if not getattr(store, "rescue_cases_by_customer", {}).get(x.get("customer_id"))]
        base["execution"].update({
            "calculations_executed": sum(len(p.get("calculations") or []) for p in customer_packs),
            "sop_clauses_retrieved": sum(len(p.get("policy_matches") or []) for p in customer_packs),
            "evidence_fingerprint": hashlib.sha256(json.dumps(matrix, sort_keys=True, default=str).encode()).hexdigest()[:16],
        })
        base.update({
            "headline": f"{focus.get('display_name','The top customer')} leads because {str(focus.get('reason','the current trigger')).lower()}.",
            "narrative": "AI explains rank using risk/service urgency first, then time-bound review needs, then growth value. No duplicate customer dossier is shown.",
            "queue": matrix,
            "observed_facts": [
                _source("Top customer", focus.get("display_name") or "—", "priority_queue"),
                _source("Priority reason", focus.get("reason") or "—", "priority_queue"),
                _source("Relationship value", focus.get("relationship_value_score", 0), "customer_master"),
                _source("Blocking documents", focus.get("blocking_documents", 0), "document_status"),
            ],
            "new_findings": ["The ranking rationale is now explicit and auditable."],
            "evidence": ["priority_queue", "ews_engine", "service_requests", "document_status", "relationship_value_score"],
            "next_gate": {"label": "Sequence the day", "reason": "The RM now knows who matters; the next problem is fitting them into an executable day."},
        })
    elif stage == 3:
        seq = plan.get("time_blocks", [])
        base.update({
            "headline": plan.get("focus_theme") or "A sequenced RM day is ready.",
            "narrative": plan.get("headline") or "Priority conversations are ordered around risk, SLA and growth constraints.",
            "day_sequence": seq,
            "observed_facts": [
                _source("Planned conversations", len(seq), "daily_planner"),
                _source("High-priority tasks", plan.get("task_load", {}).get("high_priority", 0), "crm_tasks", "risk"),
                _source("Due within 48h", plan.get("task_load", {}).get("due_within_48h", 0), "crm_tasks", "risk"),
            ],
            "new_findings": ["Risk/service work is sequenced before renewal and growth unless the RM overrides it."],
            "evidence": ["priority_queue", "crm_tasks", "sla_due_dates", "daily_planner"],
            "next_gate": {"label": "Commit actions", "reason": "A calendar sequence still needs owned preparation and follow-through."},
        })
    else:
        seq = plan.get("time_blocks", [])
        acts = []
        for row in seq[:5]:
            sol = _portfolio_solution_summary(store, row.get("customer_id"))
            steps = sol.get("steps") or []
            first = steps[0] if steps else {}
            acts.append({
                "customer_id": row.get("customer_id"), "customer": row.get("customer"), "slot": row.get("slot"),
                "action": sol.get("title"),
                "prep": first.get("action") or sol.get("description"),
                "why": sol.get("description"),
                "impact": sol.get("impact"),
                "policy_refs": sol.get("policy_refs", []),
                "solution_steps": steps,
                "solution_source": sol.get("source"),
                "crm_candidate": "Create authorised resolution workflow after RM review",
            })
        base["execution"].update({
            "calculations_executed": sum(len(build_decision_pack(store, row.get("customer_id")).get("calculations") or []) for row in seq[:5] if not getattr(store, "rescue_cases_by_customer", {}).get(row.get("customer_id"))),
            "sop_clauses_retrieved": sum(len(build_decision_pack(store, row.get("customer_id")).get("policy_matches") or []) for row in seq[:5] if not getattr(store, "rescue_cases_by_customer", {}).get(row.get("customer_id"))),
            "evidence_fingerprint": hashlib.sha256(json.dumps(acts, sort_keys=True, default=str).encode()).hexdigest()[:16],
        })
        base.update({
            "headline": f"Commit {len(acts)} customer resolution packages—not generic follow-up tasks.",
            "narrative": "Each customer action now names the bank remedy, quantified or protected outcome, applicable policy basis and first authorised decision. Nothing is auto-executed.",
            "actions": acts,
            "observed_facts": [
                _source("Open tasks", plan.get("task_load", {}).get("open_total", 0), "crm_tasks"),
                _source("High-priority tasks", plan.get("task_load", {}).get("high_priority", 0), "crm_tasks", "risk"),
                _source("Committed customer actions", len(acts), "daily_planner", "positive"),
            ],
            "new_findings": ["The morning briefing ends in customer-specific resolution packages derived from SOP and data, not generic ownership or clarification tasks."],
            "evidence": ["daily_planner", "quantitative decision packs", "SOP decision index", "validated rescue plans"],
            "next_gate": {"label": "Open a customer", "reason": "Select a customer to run the progressive relationship thesis before the conversation."},
        })
    return base


def _ai_enrich_portfolio(fallback: dict) -> dict:
    if not llm.available():
        return fallback
    evidence = {
        "stage": {k: fallback[k] for k in ("stage", "stage_id", "title", "question", "capability")},
        "portfolio_counts": fallback.get("portfolio_counts", {}),
        "queue": fallback.get("queue", []),
        "day_sequence": fallback.get("day_sequence", []),
        "actions": fallback.get("actions", []),
        "observed_facts": fallback.get("observed_facts", []),
        "guardrail": fallback.get("guardrail"),
    }
    schema = ('{"headline":"one executive headline","narrative":"2-3 concise sentences",'
              '"confidence":0.0,"uncertainty":"what may change the plan","new_findings":["stage-only findings"]}')
    task = (
        f"Generate stage {fallback['stage']} of a progressive RM morning-briefing story. Answer only the stage question. "
        "Do not repeat every customer fact and do not generate downstream talk tracks early. Explain the deterministic "
        "ranking or sequence without changing it. Keep the result concise and suitable for a stakeholder demo."
    )
    try:
        ai = llm.narrate_json(task, evidence, schema, temperature=0.3, max_tokens=600)
        out = deepcopy(fallback)
        out["headline"] = str(ai.get("headline") or fallback["headline"])
        out["narrative"] = str(ai.get("narrative") or fallback["narrative"])
        out["confidence"] = max(0.0, min(1.0, _num(ai.get("confidence"), fallback["confidence"])))
        out["uncertainty"] = str(ai.get("uncertainty") or fallback["uncertainty"])
        if isinstance(ai.get("new_findings"), list) and ai["new_findings"]:
            out["new_findings"] = [str(x) for x in ai["new_findings"][:4]]
        out["generated_by"] = "llm_grounded"
        out["generated_at"] = _now()
        execution = dict(out.get("execution") or {})
        execution.update({
            "llm_invoked": True,
            "mode": "Runtime AI portfolio synthesis",
            "model": "Contoso AI engine",
            "cache_hit": False,
        })
        out["execution"] = execution
        return out
    except Exception as exc:
        out = deepcopy(fallback)
        execution = dict(out.get("execution") or {})
        execution["fallback_reason"] = exc.__class__.__name__
        out["execution"] = execution
        return out


def portfolio_story_stage(store: DataStore, rm_id: str = "RM-2207", stage: int = 1,
                          focus_customer_id: str | None = None, force: bool = False) -> dict:
    if stage < 1 or stage > len(PORTFOLIO_STAGES):
        raise ValueError(f"stage must be between 1 and {len(PORTFOLIO_STAGES)}")
    fallback = _portfolio_fallback(store, rm_id, stage, focus_customer_id)
    evidence = {"rm_id": rm_id, "stage": stage, "counts": fallback["portfolio_counts"],
                "queue": fallback.get("queue", []), "sequence": fallback.get("day_sequence", []),
                "actions": fallback.get("actions", [])}
    key = _cache_key("portfolio", rm_id, stage, evidence)
    if not force and key in _CACHE:
        cached = deepcopy(_CACHE[key])
        if isinstance(cached.get("execution"), dict):
            cached["execution"]["cache_hit"] = True
        return cached
    result = _ai_enrich_portfolio(fallback)
    result["analysis_id"] = f"DAY-{rm_id}-{stage}-{key.rsplit(':',1)[-1][:8]}"
    result["stage_count"] = len(PORTFOLIO_STAGES)
    result["stages"] = PORTFOLIO_STAGES
    _CACHE[key] = deepcopy(result)
    store.add_event("ai.portfolio_story_stage", {"rm_id": rm_id, "stage": stage, "generated_by": result["generated_by"]})
    return result


def portfolio_story_status(store: DataStore, rm_id: str = "RM-2207") -> dict:
    return {
        "rm_id": rm_id, "stages": PORTFOLIO_STAGES, "stage_count": len(PORTFOLIO_STAGES),
        "ai_available": llm.available(), "architecture": "progressive-stage-gated",
    }
