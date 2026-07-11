"""
backend/app/services/rm_intelligence.py

The AI layer that makes the CRM use cases genuinely *intelligent* rather than
static templates. Every function:
  - builds a grounded EVIDENCE pack from the customer's real data (deterministic),
  - asks the Foundry LLM to reason over it (llm.narrate / narrate_json),
  - degrades gracefully to a deterministic summary if the LLM is unavailable.

Covers:
  1. relationship_thesis()        — dynamic, reasoned "why this customer, why now"
  2. assist_search()              — grounded search over CUSTOMER DATA + SOPs (PII-masked)
  3. ews_reasoning()              — India-context early-warning reasoning per signal
  4. collateral_pack()            — full, detailed personalised outreach (not a one-liner)
  5. dilo_reasoning()             — the AI rationale over the day plan / portfolio
"""
from __future__ import annotations
import re
from datetime import datetime

from app.store import DataStore
from app.services import llm
from app.services.analytics import AccountConduct, EWSEngine, EnhancementAssessor
from app.services.crosssell import opportunities
from app.services.relationship import recent_transactions, _top_counterparties


def _f(v, d=0.0):
    try:
        return float(v) if v not in (None, "") else d
    except (TypeError, ValueError):
        return d


def _inr(n) -> str:
    n = _f(n)
    if abs(n) >= 1e7:
        return f"\u20b9{n/1e7:.2f} Cr"
    if abs(n) >= 1e5:
        return f"\u20b9{n/1e5:.1f} L"
    return f"\u20b9{n:,.0f}"


# ---------------------------------------------------------------- PII masking
_PII_PATTERNS = [
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[email]"),
    (re.compile(r"\b(?:\+?91[\-\s]?)?[6-9]\d{9}\b"), "[phone]"),
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), "[PAN]"),          # PAN
    (re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z]\d[Z][A-Z\d]\b"), "[GSTIN]"),
    (re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"), "[aadhaar]"),
    (re.compile(r"\b\d{9,18}\b"), "[acct-no]"),                # long bare account numbers
]


def mask_pii(text: str) -> str:
    if not text:
        return text
    out = text
    for pat, repl in _PII_PATTERNS:
        out = pat.sub(repl, out)
    return out


def _mask_obj(obj):
    if isinstance(obj, str):
        return mask_pii(obj)
    if isinstance(obj, dict):
        return {k: _mask_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_obj(v) for v in obj]
    return obj


# ================================================================ 1. THESIS
def relationship_thesis(store: DataStore, customer_id: str) -> dict:
    cust = store.one("customer_master", customer_id=customer_id) or {}
    prof = store.one("business_profile", customer_id=customer_id) or {}
    facility = store.one("facilities", customer_id=customer_id) or {}
    conduct = AccountConduct(store, customer_id).summary()
    ews = EWSEngine(store, customer_id).signals()
    enh = EnhancementAssessor(store, customer_id).assess()
    opps = opportunities(store, customer_id)
    aging = store.one("aging", customer_id=customer_id) or {}
    fin = store.one("financials", customer_id=customer_id) or {}
    srs = [s for s in store.where("service_requests", customer_id=customer_id) if s.get("status") == "Open"]
    docs = [d for d in store.where("documents", customer_id=customer_id)
            if d.get("status") in ("Pending", "Expired", "Overdue") and d.get("required_flag") == "Y"]
    tops = _top_counterparties(store, customer_id, 5)

    evidence = {
        "identity": {
            "name": cust.get("display_name"), "constitution": cust.get("constitution"),
            "segment": cust.get("segment"), "industry": prof.get("industry") or prof.get("sector"),
            "location": cust.get("city") or cust.get("location"),
            "vintage_years": cust.get("relationship_vintage_years") or cust.get("vintage_years"),
            "risk_grade": cust.get("risk_category"), "rvs": cust.get("relationship_value_score"),
            "kyc_status": cust.get("kyc_status"),
        },
        "facility": {
            "type": facility.get("facility_type"), "limit": facility.get("sanction_limit_inr"),
            "outstanding": facility.get("current_outstanding_inr"),
            "drawing_power": facility.get("drawing_power_inr"),
            "review_due": facility.get("review_due_date"), "status": facility.get("facility_status"),
        },
        "conduct": {
            "credits_trend": f"{conduct['credits_trend_label']} {conduct['credits_trend_pct']}%",
            "avg_utilization_pct": conduct["avg_utilization_pct"],
            "peak_utilization_pct": conduct["peak_utilization_pct"],
            "cheque_returns": conduct["cheque_return_count"],
            "top_counterparty_concentration_pct": conduct.get("top_counterparty_concentration_pct"),
        },
        "financials": {"turnover_inr": fin.get("turnover_inr"), "fy": fin.get("fy")},
        "receivables_aging": aging,
        "early_warning_signals": [{"signal": s["signal_type"], "severity": s["severity"],
                                   "evidence": s["evidence_metric"]} for s in ews],
        "enhancement_view": {"eligible": enh.get("eligible"), "reason": enh.get("reason") or enh.get("rationale")},
        "opportunities": [{"product": o.get("product"), "eligible": o.get("eligible")} for o in opps],
        "open_service_requests": [{"id": s.get("ticket_id"), "category": s.get("category"),
                                   "priority": s.get("priority")} for s in srs],
        "blocking_documents": [{"doc": d.get("document_type"), "status": d.get("status")} for d in docs],
        "top_counterparties": tops,
    }

    fallback = _thesis_fallback(cust, conduct, ews, enh, srs, docs)
    fp = {"signals": len(ews), "open_srs": len(srs), "blocking_docs": len(docs),
          "eligible_opps": len([o for o in opps if o.get("eligible")])}
    if not llm.available():
        return {"customer_id": customer_id, "generated_by": "deterministic_fallback",
                "evidence_footprint": fp, **fallback}

    schema = (
        '{"headline": "one punchy sentence — the relationship in a nutshell",'
        ' "thesis": "2-3 sentence narrative: who they are, how they bank with us, the trajectory",'
        ' "why_now": "what changed / what makes today the moment to act",'
        ' "posture": "Grow | Protect | Stabilise | Watch",'
        ' "top_actions": [{"action": "...", "rationale": "...", "urgency": "High|Medium|Low"}],'
        ' "risk_read": "the single most important risk sentence",'
        ' "opportunity_read": "the single most important opportunity sentence"}'
    )
    task = (
        "You are briefing the RM on this MSME customer at the start of the day. Using ONLY the evidence, "
        "write a sharp, India-MSME-banking relationship thesis. Think like a credit-aware RM: working-capital "
        "cycle, utilisation vs drawing power, conduct trend, covenant/doc compliance, receivables quality, and "
        "wallet-share. The 'posture' must follow the data (a stressed account is Protect/Stabilise, not Grow). "
        "top_actions: 3 concrete, sequenced next steps. Be specific and quantified; never invent numbers."
    )
    try:
        out = llm.narrate_json(task, evidence, schema, temperature=0.4, max_tokens=900)
        out["customer_id"] = customer_id
        out["generated_by"] = "llm_grounded"
        out["evidence_footprint"] = {
            "signals": len(ews), "open_srs": len(srs), "blocking_docs": len(docs),
            "eligible_opps": len([o for o in opps if o.get("eligible")]),
        }
        return out
    except Exception:
        return {"customer_id": customer_id, "generated_by": "deterministic_fallback", **fallback}


def _thesis_fallback(cust, conduct, ews, enh, srs, docs) -> dict:
    stressed = any(s["severity"] in ("Critical", "High") for s in ews) or conduct["credits_trend_pct"] < 0
    posture = "Protect" if stressed else ("Grow" if enh.get("eligible") else "Watch")
    return {
        "headline": f"{cust.get('display_name','Customer')} — {cust.get('risk_category','?')} risk, "
                    f"credits {conduct['credits_trend_label']} {conduct['credits_trend_pct']}%.",
        "thesis": f"{cust.get('display_name','Customer')} banks with us on a {cust.get('segment','MSME')} "
                  f"relationship; utilisation averages {conduct['avg_utilization_pct']}% with "
                  f"{conduct['cheque_return_count']} cheque return(s) recorded.",
        "why_now": (f"{len(srs)} open service request(s) and {len(docs)} blocking document(s) need attention."
                    if (srs or docs) else "Routine review; maintain monitoring."),
        "posture": posture,
        "top_actions": [{"action": "Review open items", "rationale": "Service/doc items outstanding", "urgency": "High" if (srs or docs) else "Low"}],
        "risk_read": (ews[0]["signal_type"] + " — " + ews[0]["evidence_metric"]) if ews else "No material signals.",
        "opportunity_read": ("Stabilise the account before pursuing growth." if stressed
                             else "Account supports a review; assess eligible cross-sell."),
    }


# ================================================================ 2. ASSIST SEARCH (customer + SOP, PII-masked)
def assist_search(store: DataStore, customer_id: str, query: str, scope: str = "customer") -> dict:
    """Grounded RM-assist search. Retrieves from the customer's OWN data (PII-masked)
    and, where available, the SOP/policy index, then has the LLM answer ONLY from
    those sources with citations. `scope` controls emphasis:
      customer -> lead with the customer's position
      product  -> lead with product fit for this customer
      policy   -> lead with the SOP/policy answer
      all      -> integrate customer + product + policy."""
    cust = store.one("customer_master", customer_id=customer_id) or {}
    conduct = AccountConduct(store, customer_id).summary()
    facility = store.one("facilities", customer_id=customer_id) or {}
    covenants = store.where("covenants", customer_id=customer_id)
    docs = store.where("documents", customer_id=customer_id)
    srs = store.where("service_requests", customer_id=customer_id)
    aging = store.one("aging", customer_id=customer_id) or {}
    fin = store.one("financials", customer_id=customer_id) or {}
    gst = sorted(store.where("gst", customer_id=customer_id), key=lambda r: r.get("period", ""))[-6:]
    opps = opportunities(store, customer_id)
    txns = recent_transactions(store, customer_id, 10)

    # customer evidence (PII-masked)
    customer_corpus = _mask_obj({
        "profile": {"name": cust.get("display_name"), "constitution": cust.get("constitution"),
                    "segment": cust.get("segment"), "risk_grade": cust.get("risk_category"),
                    "rvs": cust.get("relationship_value_score"), "kyc_status": cust.get("kyc_status")},
        "facility": facility, "covenants": covenants, "documents": docs,
        "service_requests": srs, "receivables_aging": aging, "financials": fin,
        "gst_recent": gst, "conduct": conduct, "opportunities": opps,
        "recent_transactions": txns,
    })

    # SOP/policy corpus — try the search index; degrade to the local KB if unavailable
    sop_hits = _retrieve_sops(store, query)
    # product catalogue (for product / all scopes)
    products = []
    if scope in ("product", "all"):
        try:
            products = [{"product_id": o.get("product_id"), "product": o.get("product"),
                         "eligible": o.get("eligible"), "rationale": o.get("rationale"),
                         "category": o.get("category")} for o in opps]
        except Exception:
            products = []

    fallback = _search_fallback(query, customer_corpus, sop_hits)
    if not llm.available():
        return {"customer_id": customer_id, "query": query, "scope": scope,
                "generated_by": "deterministic_fallback", **fallback}

    scope_emphasis = {
        "customer": "Lead with THIS customer's position; bring policy only if it bears on the answer.",
        "product": "Lead with which product best fits THIS customer and why, grounded in their data; cite the catalogue.",
        "policy": "Lead with the SOP/policy answer and cite the clause; then note how it applies to this customer.",
        "all": "Integrate the customer's position, product fit and policy into one coherent answer.",
    }.get(scope, "Lead with the customer's position.")

    evidence = {"question": query, "lens": scope, "customer_data_pii_masked": customer_corpus,
                "product_catalogue": products, "policy_sop_excerpts": sop_hits}
    schema = (
        '{"answer": "direct, specific answer to the question grounded in the evidence",'
        ' "customer_specifics": ["concrete facts from THIS customer that bear on the answer"],'
        ' "policy_points": ["relevant SOP/policy points, if any, each ending with its source id"],'
        ' "caveats": ["eligibility/consent/doc conditions the RM must check"],'
        ' "citations": ["source ids used — sop ids and/or customer data tables"]}'
    )
    task = (
        "Answer the RM's question about THIS customer. " + scope_emphasis + " Use ONLY the evidence: the "
        "customer's own (PII-masked) data, the product catalogue, and any policy excerpts. Be a credit-aware "
        "Indian MSME banker — connect the customer's actual position (utilisation, covenants, aging, conduct) "
        "to the question, and bring in policy where relevant with its source id. If the evidence doesn't "
        "support an answer, say so plainly. Never invent figures or quote a policy not in the excerpts."
    )
    try:
        out = llm.narrate_json(task, evidence, schema, temperature=0.3, max_tokens=900)
        out.update({"customer_id": customer_id, "query": query, "scope": scope, "generated_by": "llm_grounded",
                    "sources_searched": {"customer_tables": True, "product_catalogue": len(products), "sop_chunks": len(sop_hits)}})
        return out
    except Exception:
        return {"customer_id": customer_id, "query": query, "scope": scope,
                "generated_by": "deterministic_fallback", **fallback}


def _retrieve_sops(store: DataStore, query: str) -> list[dict]:
    # 1) try the Azure Search index (production path)
    try:
        from app.services.search import retrieve
        r = retrieve(query, top_k=4)
        hits = r.get("results") or r.get("hits") or []
        if hits:
            return [{"source": h.get("sop_id") or h.get("sop_title"),
                     "title": h.get("sop_title"), "section": h.get("section_title"),
                     "content": (h.get("content") or "")[:600]} for h in hits]
    except Exception:
        pass
    # 2) degrade to local knowledge-base CSV/markdown keyword scan
    try:
        kb = store.where("sop_chunks") or store.where("knowledge_base") or []
        ql = set(re.findall(r"\w+", query.lower()))
        scored = []
        for c in kb:
            text = " ".join(str(v) for v in c.values()).lower()
            score = sum(1 for w in ql if w in text)
            if score:
                scored.append((score, c))
        scored.sort(key=lambda x: -x[0])
        return [{"source": c.get("sop_id") or c.get("id") or "SOP",
                 "title": c.get("sop_title") or c.get("title"),
                 "section": c.get("section_title"),
                 "content": (c.get("content") or c.get("text") or "")[:600]} for _, c in scored[:4]]
    except Exception:
        return []


def _search_fallback(query, customer_corpus, sop_hits) -> dict:
    return {
        "answer": ("LLM unavailable — showing the retrieved evidence. Review the customer specifics and any "
                   "policy excerpts below to answer: " + query),
        "customer_specifics": [f"Facility: {customer_corpus.get('facility', {}).get('facility_type')} "
                               f"limit {customer_corpus.get('facility', {}).get('sanction_limit_inr')}",
                               f"KYC: {customer_corpus.get('profile', {}).get('kyc_status')}"],
        "policy_points": [f"{h.get('title')} ({h.get('source')})" for h in sop_hits],
        "caveats": ["Verify against current SOP and customer consent before acting."],
        "citations": [h.get("source") for h in sop_hits],
    }


# ================================================================ 3. EWS REASONING (India context)
def ews_reasoning(store: DataStore, customer_id: str) -> dict:
    cust = store.one("customer_master", customer_id=customer_id) or {}
    conduct = AccountConduct(store, customer_id).summary()
    ews = EWSEngine(store, customer_id).signals()
    facility = store.one("facilities", customer_id=customer_id) or {}
    aging = store.one("aging", customer_id=customer_id) or {}
    covenants = store.where("covenants", customer_id=customer_id)
    fin = store.one("financials", customer_id=customer_id) or {}

    evidence = {
        "customer": {"name": cust.get("display_name"), "segment": cust.get("segment"),
                     "risk_grade": cust.get("risk_category"), "industry": cust.get("industry")},
        "signals": [{"signal": s["signal_type"], "severity": s["severity"], "evidence": s["evidence_metric"],
                     "guardrail": s.get("false_positive_guardrail")} for s in ews],
        "conduct": conduct, "facility": facility, "receivables_aging": aging,
        "covenants": covenants, "financials": fin,
    }
    fallback = {
        "overall_read": (f"{len(ews)} signal(s) active; highest severity "
                         f"{'Critical' if any(s['severity']=='Critical' for s in ews) else ('High' if any(s['severity']=='High' for s in ews) else 'Medium' if ews else 'None')}."),
        "signal_reasoning": [{"signal": s["signal_type"], "why_it_matters": s["evidence_metric"],
                              "ask": "Seek clarification with the customer."} for s in ews],
        "sma_view": "Monitor utilisation and credits; escalate if the trend persists.",
        "next_step": "Address the highest-severity signal first.",
        "generated_by": "deterministic_fallback", "customer_id": customer_id,
    }
    if not ews or not llm.available():
        return fallback
    schema = (
        '{"overall_read": "one-paragraph portfolio-risk read of this account",'
        ' "sma_view": "SMA/asset-quality view in RBI terms (SMA-0/1/2 framing where the data supports it)",'
        ' "signal_reasoning": [{"signal": "...", "why_it_matters": "India-MSME credit reasoning",'
        '   "likely_benign_cause": "...", "likely_risk_cause": "...", "clarification_to_seek": "..."}],'
        ' "next_step": "the single most important action this week"}'
    )
    task = (
        "Explain these early-warning signals to the RM as an experienced Indian MSME credit officer would. "
        "For EACH signal give the benign vs risk reading side by side and the clarification to seek (never "
        "allege wrongdoing). Frame asset quality in RBI SMA terms (SMA-0: 1-30 dpd-equivalent stress, SMA-1: "
        "31-60, SMA-2: 61-90) where utilisation/credits/returns support it. Consider India-specific MSME "
        "realities: cash-intensive trade, delayed receivables from large buyers, seasonal/festival cycles, "
        "GST-vs-banked-sales gaps, promoter-funded working capital. Ground every point in the evidence."
    )
    try:
        out = llm.narrate_json(task, evidence, schema, temperature=0.4, max_tokens=1100)
        out.update({"customer_id": customer_id, "generated_by": "llm_grounded", "signal_count": len(ews)})
        return out
    except Exception:
        return fallback


# ================================================================ 4. COLLATERAL (rich, detailed)
def _emi(principal: float, annual_rate_pct: float, months: int) -> float:
    p = float(principal or 0); n = int(months or 0); r = float(annual_rate_pct or 0) / 1200.0
    if p <= 0 or n <= 0:
        return 0.0
    if r == 0:
        return p / n
    return p * r * (1 + r) ** n / ((1 + r) ** n - 1)


def _retail_collateral(pack: dict, pid: str, name: str) -> dict:
    """Deterministic, NUMBER-RICH email content per retail product, computed from
    the evidence pack. Returns subject/body/comparison/talking_points/objection_handling
    so the email always carries real figures (never just words)."""
    from app.services.collateral import _inr, _f
    fac = pack.get("facility", {}) or {}
    cs = pack.get("credit_score", {}) or {}
    loans = pack.get("loans", []) or []
    turn = pack.get("turnover", {}) or {}
    sav = pack.get("savings", {}) or {}
    col = pack.get("collateral", {}) or {}
    score = cs.get("score")
    mi = (_f(turn.get("fy_credits_inr")) or 0) / 12.0          # ~monthly inflow
    hl = next((l for l in loans if l.get("type") == "Home Loan"), None)
    pl = next((l for l in loans if l.get("type") == "Personal Loan"), None)
    sign = "Warm regards,\nRelationship Manager, Contoso Bank"

    if pid == "PRD-CC-EMI":
        O = _f(fac.get("outstanding_inr")); apr = _f(fac.get("interest_rate_pct")) or 42
        conv, n = 16.0, 12
        emi = _emi(O, conv, n); emi_int = max(0.0, emi * n - O); revolve = O * apr / 100.0
        saving = max(0.0, revolve - emi_int); util = fac.get("utilisation_avg_30d_pct")
        subject = f"Cut the interest on your {_inr(O)} card balance — convert it to EMI"
        body = (
            f"Dear {name},\n\nYour credit-card balance of {_inr(O)} is currently revolving at {apr}% p.a. — "
            f"that works out to roughly {_inr(revolve)} a year in interest if it keeps rolling.\n\n"
            f"Here's the breakdown if we convert it to a {n}-month EMI at ~{conv}% p.a.:\n"
            f"  • Monthly EMI: ~{_inr(emi)}\n"
            f"  • Total interest over {n} months: ~{_inr(emi_int)}\n"
            f"  • Estimated saving vs revolving: ~{_inr(saving)}\n"
            f"  • Your card utilisation ({util}%) starts falling immediately, which helps your CIBIL.\n\n"
            f"Shall I set up the EMI conversion on this call? It's reversible — you can foreclose later.\n\n{sign}"
        )
        comparison = [
            {"dimension": "Interest rate", "without": f"{apr}% p.a. (revolving)", "with": f"~{conv}% p.a. (EMI)"},
            {"dimension": "Monthly outgo", "without": "minimum due — balance compounds", "with": f"fixed {_inr(emi)} for {n} months"},
            {"dimension": "Approx. annual interest", "without": _inr(revolve), "with": _inr(emi_int)},
            {"dimension": "Payoff", "without": "open-ended", "with": f"cleared in {n} months"},
            {"dimension": "Card utilisation / CIBIL", "without": f"{util}% (drags the score)", "with": "falls as the balance amortises"},
        ]
        talking_points = [
            f"Your {_inr(O)} balance at {apr}% is ~{_inr(revolve)}/year in interest.",
            f"A {n}-month EMI at ~{conv}% is ~{_inr(emi)}/month, ~{_inr(emi_int)} total interest — saving ~{_inr(saving)}.",
            f"Utilisation drops from {util}%, which helps CIBIL ({score}) recover over the next cycles.",
        ]
        objection_handling = [
            {"objection": "Will this hurt my credit score?", "response": "No — moving a revolving balance to a structured EMI lowers utilisation, which typically helps your score."},
            {"objection": "Can I foreclose later?", "response": "Yes; you can foreclose the EMI plan and we'll confirm any small foreclosure terms upfront."},
        ]
    elif pid == "PRD-PL-RESTRUCT":
        O = _f((pl or {}).get("outstanding_inr")); cur_emi = _f((pl or {}).get("emi_inr")); rate = _f((pl or {}).get("rate_pct")) or 16.5
        n2 = 48; new_emi = _emi(O, rate, n2)
        relief = max(0.0, cur_emi - new_emi)
        subject = "Let's ease your loan EMIs and protect your credit score"
        body = (
            f"Dear {name},\n\nI can see two EMIs have bounced on your personal loan (outstanding {_inr(O)}, current EMI {_inr(cur_emi)}). "
            f"Before this affects your CIBIL further, here's a restructuring option:\n\n"
            f"  • Step-down/extend tenure to ~{n2} months at {rate}%\n"
            f"  • Revised EMI: ~{_inr(new_emi)} (about {_inr(relief)} lower each month)\n"
            f"  • One-time deferral of the bounced EMIs can be arranged with documented hardship\n\n"
            f"This keeps the account regular and protects your score. Can we start the restructuring today?\n\n{sign}"
        )
        comparison = [
            {"dimension": "Monthly EMI", "without": _inr(cur_emi), "with": f"~{_inr(new_emi)}"},
            {"dimension": "Monthly relief", "without": "—", "with": _inr(relief)},
            {"dimension": "Account status", "without": "SMA-1 (bounces hurting CIBIL)", "with": "regularised; score protected"},
        ]
        talking_points = [
            f"Two EMIs bounced; outstanding {_inr(O)}, current EMI {_inr(cur_emi)}.",
            f"A step-down to ~{n2} months cuts the EMI to ~{_inr(new_emi)} (~{_inr(relief)} relief/month).",
            "Restructuring keeps the account regular and stops further CIBIL damage — better than new credit.",
        ]
        objection_handling = [
            {"objection": "Will restructuring show on my credit report?", "response": "It is noted, but it is far better than continued bounces or default, which damage the score more."},
            {"objection": "Can you waive the bounce charges?", "response": "I'll raise a fee review with documented hardship; I can't promise a waiver, but I'll log and pursue it."},
        ]
    elif pid == "PRD-PL-PREAPP":
        amt = max(500000.0, min(800000.0, round(mi * 3 / 10000.0) * 10000.0)); rate, n = 10.5, 48
        emi = _emi(amt, rate, n)
        subject = f"You're pre-approved for a personal loan up to {_inr(amt)}"
        body = (
            f"Dear {name},\n\nWith a CIBIL of {score} and income of {turn.get('annual_income_text')}, you qualify for a pre-approved personal loan.\n\n"
            f"Indicative offer:\n  • Amount: up to {_inr(amt)}\n  • Rate: ~{rate}% p.a. (your CIBIL slab)\n"
            f"  • EMI: ~{_inr(emi)}/month over {n} months\n  • Disbursal: 24-48h after KYC, no collateral\n\n"
            f"Shall I share the formal offer? Final terms are confirmed on application.\n\n{sign}"
        )
        comparison = [
            {"dimension": "Eligibility", "without": "—", "with": f"Pre-approved up to {_inr(amt)}"},
            {"dimension": "Rate", "without": "card/other credit at higher rates", "with": f"~{rate}% p.a."},
            {"dimension": "EMI", "without": "—", "with": f"{_inr(emi)}/month over {n} months"},
            {"dimension": "Disbursal", "without": "—", "with": "24-48h after KYC, no collateral"},
        ]
        talking_points = [
            f"CIBIL {score} and income {turn.get('annual_income_text')} → pre-approved up to {_inr(amt)}.",
            f"~{rate}% over {n} months is about {_inr(emi)}/month.",
            "Fully digital; funds in 24-48h after KYC, no collateral or guarantor.",
        ]
        objection_handling = [
            {"objection": "Is the rate fixed?", "response": "Indicative; the final rate is confirmed on application, but your profile qualifies for our best slab."},
            {"objection": "Any hidden charges?", "response": "Only a standard, disclosed processing fee; no prepayment penalty on the floating-rate option."},
        ]
    elif pid == "PRD-CC-LIMIT":
        cur = _f(fac.get("sanction_limit_inr")); new = round(cur * 1.75 / 10000.0) * 10000.0; util = fac.get("utilisation_avg_30d_pct")
        subject = f"A higher limit for your card — {_inr(cur)} to {_inr(new)}"
        body = (
            f"Dear {name},\n\nYour card runs at just {util}% utilisation with strong, regular spends and a CIBIL of {score}. "
            f"You qualify for a limit increase and a premium upgrade.\n\n"
            f"  • Current limit: {_inr(cur)}\n  • Proposed limit: up to {_inr(new)}\n  • Premium travel/rewards tier\n\n"
            f"Shall I apply the upgrade? Subject to standard card policy.\n\n{sign}"
        )
        comparison = [
            {"dimension": "Credit limit", "without": _inr(cur), "with": f"up to {_inr(new)}"},
            {"dimension": "Utilisation", "without": f"{util}%", "with": "lower — more headroom for big spends"},
            {"dimension": "Tier", "without": "current card", "with": "premium travel/rewards"},
        ]
        talking_points = [
            f"Utilisation is only {util}% with a CIBIL of {score} — clear headroom for an increase.",
            f"Limit can move from {_inr(cur)} to about {_inr(new)}.",
            "Premium upgrade adds travel/rewards benefits at the same relationship.",
        ]
        objection_handling = [
            {"objection": "Will my interest go up?", "response": "No — the rate is unchanged; only your limit and benefits improve."},
            {"objection": "Do I have to pay a fee?", "response": "Any premium-tier fee is disclosed upfront and is often waived on spend milestones."},
        ]
    elif pid == "PRD-HL-TOPUP":
        out = _f((hl or {}).get("outstanding_inr")); val = _f(col.get("valuation_inr"))
        headroom = max(0.0, val * 0.8 - out); topup = min(headroom, 1500000.0) if headroom else 1000000.0
        rate, n = 8.75, 120; emi = _emi(topup, rate, n)
        subject = f"A home-loan top-up of up to {_inr(topup)} at {rate}%"
        body = (
            f"Dear {name},\n\nYour home loan (outstanding {_inr(out)}) is on track, and the property is valued at {_inr(val)}. "
            f"That leaves comfortable equity for a top-up at home-loan rates — far cheaper than a personal loan or card.\n\n"
            f"  • Indicative top-up: up to {_inr(topup)}\n  • Rate: ~{rate}% p.a.\n  • EMI: ~{_inr(emi)}/month over {n} months\n\n"
            f"Useful for renovation or consolidating costlier debt. Shall I compute your exact eligibility?\n\n{sign}"
        )
        comparison = [
            {"dimension": "Rate", "without": "personal loan/card (12-42%)", "with": f"~{rate}% (home-loan rate)"},
            {"dimension": "Available top-up", "without": "—", "with": f"up to {_inr(topup)}"},
            {"dimension": "EMI", "without": "—", "with": f"{_inr(emi)}/month over {n} months"},
        ]
        talking_points = [
            f"Home loan outstanding {_inr(out)} against a {_inr(val)} property — healthy equity.",
            f"Top-up up to {_inr(topup)} at ~{rate}%, about {_inr(emi)}/month.",
            "Much cheaper than a personal loan or revolving card for the same need.",
        ]
        objection_handling = [
            {"objection": "Will my current EMI change?", "response": "Only the top-up adds an EMI; your existing schedule continues unless you choose to merge them."},
            {"objection": "Is a fresh valuation needed?", "response": "Usually a quick desktop revaluation; we'll confirm if a full valuation is required."},
        ]
    elif pid == "PRD-WEALTH":
        bal = _f(sav.get("avg_balance_inr")); surplus = max(0.0, bal - 200000.0)
        sip = max(5000.0, round(mi * 0.10 / 1000.0) * 1000.0); fv = sip * (((1 + 0.11/12) ** 60 - 1) / (0.11/12))
        subject = "Putting your surplus to work — a simple SIP + FD plan"
        body = (
            f"Dear {name},\n\nYou're holding an average balance of {_inr(bal)} in savings earning ~3%. "
            f"With income rising, here's a simple way to make about {_inr(surplus)} of surplus work harder:\n\n"
            f"  • Start/step-up a SIP of ~{_inr(sip)}/month\n  • At ~11% p.a., that's about {_inr(fv)} in 5 years\n"
            f"  • Park the rest in a laddered FD for liquidity + better-than-savings returns\n\n"
            f"Shall we build the plan together? Investments are subject to market risk.\n\n{sign}"
        )
        comparison = [
            {"dimension": "Idle savings", "without": f"{_inr(bal)} at ~3%", "with": "deployed into SIP + laddered FD"},
            {"dimension": "5-year value (SIP)", "without": "—", "with": f"~{_inr(fv)} (at ~11%, illustrative)"},
            {"dimension": "Liquidity", "without": "all in savings", "with": "FD ladder keeps part accessible"},
        ]
        talking_points = [
            f"~{_inr(bal)} sitting at ~3% — about {_inr(surplus)} could be deployed.",
            f"A SIP of ~{_inr(sip)}/month could grow to ~{_inr(fv)} in 5 years (illustrative at 11%).",
            "Laddered FD keeps liquidity while beating the savings rate.",
        ]
        objection_handling = [
            {"objection": "Is my money safe?", "response": "FDs are capital-safe; SIPs carry market risk — we'll match the mix to your comfort and goals."},
            {"objection": "Can I stop the SIP anytime?", "response": "Yes, SIPs are flexible — pause, step up or stop without penalty."},
        ]
    elif pid == "PRD-INSURE":
        annual = _f(turn.get("fy_credits_inr")); cover = round(annual * 12 / 100000.0) * 100000.0 or 5000000.0
        prem = round(cover * 0.0004 / 100.0) * 100.0
        subject = "Is your cover keeping up with your income?"
        body = (
            f"Dear {name},\n\nAs your income grows, your protection should keep pace. A term cover of about {_inr(cover)} "
            f"(roughly 10-12x annual income) safeguards your family's commitments.\n\n"
            f"  • Indicative cover: {_inr(cover)}\n  • Indicative premium: ~{_inr(prem)}/year\n  • Add a health top-up for hospitalisation\n\n"
            f"Shall I share a quick quote? Suitability and a free-look period apply.\n\n{sign}"
        )
        comparison = [
            {"dimension": "Life cover", "without": "below income needs / none", "with": f"~{_inr(cover)} (10-12x income)"},
            {"dimension": "Indicative premium", "without": "—", "with": f"~{_inr(prem)}/year"},
            {"dimension": "Health", "without": "exposure to hospitalisation cost", "with": "top-up cover added"},
        ]
        talking_points = [
            f"Recommended term cover ~{_inr(cover)} (10-12x your income).",
            f"Indicative premium ~{_inr(prem)}/year for that cover.",
            "Protection is needs-based, not an investment — no pressure, just suitability.",
        ]
        objection_handling = [
            {"objection": "I already have some cover.", "response": "Great — we'll only top up the gap to match your current income, not duplicate it."},
            {"objection": "Is this bundled with a loan?", "response": "No — it's a standalone protection product you can take or decline freely."},
        ]
    else:
        subject = f"A tailored option for you, {name}"
        body = (
            f"Dear {name},\n\nBased on your profile — CIBIL {score}, income {turn.get('annual_income_text')}, "
            f"card limit {fac.get('sanction_limit_text')} at {fac.get('utilisation_avg_30d_pct')}% utilisation — "
            f"I'd like to discuss a suitable next step. Shall we set up a short call?\n\n{sign}"
        )
        comparison = [{"dimension": "Profile", "without": "—", "with": f"CIBIL {score}, income {turn.get('annual_income_text')}"}]
        talking_points = [f"CIBIL {score}, income {turn.get('annual_income_text')}, card at {fac.get('utilisation_avg_30d_pct')}% utilisation."]
        objection_handling = [{"objection": "Why are you calling?", "response": "A quick, no-obligation review of options that fit your current profile."}]
    return {"subject": subject, "body": body, "comparison": comparison,
            "talking_points": talking_points, "objection_handling": objection_handling}


def collateral_pack(store: DataStore, customer_id: str, product_id: str | None = None) -> dict:
    from app.services.collateral import build_evidence_pack
    try:
        pack = build_evidence_pack(store, customer_id)
    except Exception:
        pack = {}
    cust = store.one("customer_master", customer_id=customer_id) or {}
    name = cust.get("display_name", "Customer")
    opps = [o for o in opportunities(store, customer_id) if o.get("eligible")]
    if product_id:
        opps = [o for o in opps if o.get("product_id") == product_id] or opps
    target = opps[0] if opps else None
    pid = (target.get("product_id") if target else product_id) or ""

    # Deterministic, NUMBER-RICH content (always carries the real figures).
    content = _retail_collateral(pack, pid, name)
    result = {
        "customer_id": customer_id,
        "product": (target.get("product") if target else None),
        "product_id": pid or None,
        "subject": content["subject"], "body": content["body"],
        "comparison": content["comparison"], "talking_points": content["talking_points"],
        "objection_handling": content["objection_handling"],
        "eligibility_note": "Indicative only; final terms subject to eligibility check and KYC. No approval/pricing committed.",
        "generated_by": "evidence_computed",
    }

    # Optional LLM polish of the BODY prose only — keep all the deterministic
    # figures and the structured panels intact (and never block on the LLM).
    if target and llm.available():
        try:
            polished = llm.narrate(
                "Rewrite this retail-banking outreach email so it reads warmly and professionally for an Indian "
                "individual customer. You MUST keep every number, the bullet breakdown, and the call to action exactly "
                "as given — do not drop or change any figure. Return ONLY the email body text.",
                {"draft_body": content["body"], "key_figures": content["talking_points"], "customer_name": name},
                temperature=0.4, max_tokens=600,
            )
            if polished and len(polished) > 120 and any(ch.isdigit() for ch in polished):
                result["body"] = polished
                result["generated_by"] = "llm_grounded"
        except Exception:
            pass
    return result


# ================================================================ 5. DILO REASONING
def dilo_reasoning(store: DataStore, rm_id: str = "RM-1042") -> dict:
    from app.services.daily_planner import build_dilo, build_milo
    dilo = build_dilo(store, rm_id)
    milo = build_milo(store, rm_id)
    plan = dilo.get("plan") or dilo.get("blocks") or []

    evidence = {
        "day_plan": [{"customer": p.get("customer_name") or p.get("display_name"),
                      "why_queued": p.get("reason") or p.get("priority_reason"),
                      "priority": p.get("priority"), "rvs": p.get("relationship_value_score")} for p in plan[:8]],
        "portfolio": {k: milo.get(k) for k in ("headline", "metrics", "summary") if k in milo},
        "headline": dilo.get("headline"),
    }
    fallback = {
        "narrative": dilo.get("headline", "Day plan prioritised by value, risk and due-dates."),
        "sequencing_logic": "Accounts are ranked by relationship value, active risk and items due within 48h.",
        "portfolio_read": milo.get("headline", ""),
        "generated_by": "deterministic_fallback", "rm_id": rm_id,
    }
    if not plan or not llm.available():
        return {**dilo, "ai": fallback}
    schema = (
        '{"narrative": "2-3 sentences: how to run the day and why this order",'
        ' "sequencing_logic": "the reasoning behind the priority order — value vs risk vs urgency tradeoff",'
        ' "watchlist_focus": "which 1-2 accounts most need attention today and why",'
        ' "portfolio_read": "what the portfolio (MILO) view is signalling this month"}'
    )
    task = (
        "You are the RM's chief-of-staff. Given the prioritised day plan and the portfolio view, explain — as "
        "a sales-and-risk manager would — how to run the day and WHY this sequence: which accounts are revenue "
        "plays, which are risk/retention, and how to trade off a busy day. Ground every claim in the evidence."
    )
    try:
        ai = llm.narrate_json(task, evidence, schema, temperature=0.4, max_tokens=700)
        ai.update({"rm_id": rm_id, "generated_by": "llm_grounded"})
        return {**dilo, "ai": ai}
    except Exception:
        return {**dilo, "ai": fallback}


# ================================================================ 6. PERSONA CONVERSATION PATHS
def persona_paths(store: DataStore, customer_id: str, stakeholder_id: str) -> dict:
    """Three grounded conversation simulations for ONE stakeholder — happy, neutral and
    friction — each driven by THIS customer's real data and THIS persona's disposition.
    The whole point: the dialogue references the customer's actual conduct, covenants,
    cases and numbers, not generic banker-speak. That grounding is the AI play."""
    stk = store.one("stakeholders", customer_id=customer_id, stakeholder_id=stakeholder_id)
    if not stk:
        return {"error": f"stakeholder {stakeholder_id} not found"}
    cust = store.one("customer_master", customer_id=customer_id) or {}
    conduct = AccountConduct(store, customer_id).summary()
    ews = EWSEngine(store, customer_id).signals()
    facility = store.one("facilities", customer_id=customer_id) or {}
    enh = EnhancementAssessor(store, customer_id).assess()
    opps = [o for o in opportunities(store, customer_id) if o.get("eligible")]
    srs = [s for s in store.where("service_requests", customer_id=customer_id) if s.get("status") == "Open"]
    docs = [d for d in store.where("documents", customer_id=customer_id)
            if d.get("status") in ("Pending", "Expired", "Overdue") and d.get("required_flag") == "Y"]
    aging = store.one("aging", customer_id=customer_id) or {}
    tops = _top_counterparties(store, customer_id, 3)

    # ---- role altitude: WHAT the RM raises and HOW depends on the person's seniority/function
    title = (stk.get("title") or "").lower()
    role = (stk.get("decision_role") or "").lower()
    is_finance = any(k in title for k in ["cfo", "finance", "accounts", "accountant", "treasur", "compliance"])
    is_ops = any(k in title for k in ["operations", "procurement", "purchase", "imports", "sales", "collections", "logistics", "plant", "works"])
    # "pure" leadership = senior with no explicit finance/ops function in the title
    is_pure_leader = (any(k in title for k in ["managing director", "managing partner", "director", "promoter",
                          "proprietor", "ceo", "founder", "chairman", "owner"]) or "final approver" in role) \
        and not (is_finance or is_ops)
    # A finance/ops *leader* (CFO, Finance Partner, Head of Operations) still talks detail,
    # but with a decision-maker's framing — we treat function first so the agenda fits.
    if is_pure_leader:
        altitude = "SENIOR_DECISION_MAKER"
        agenda = ("Keep it STRATEGIC and relationship-level. This is the promoter / final decision-maker. The RM's "
                  "job here is to discuss: where the relationship stands, the renewal as a business decision, the "
                  "bank's intent to keep supporting them, the BROAD risk picture in headline terms, and what the "
                  "bank needs from them as the principal (mainly: their commitment to get their team to close the "
                  "open items, and their read on the business direction).\n"
                  "DO talk about: the renewal and what a smooth outcome needs; the bank's continued support; the "
                  "headline health of the account ('running close to the limit', 'credits down about a third'); "
                  "the business trajectory and how the bank can help; reducing dependence on the top buyer at a "
                  "strategic level.\n"
                  "DO NOT: read out individual cheque-return counts, insufficient-funds tallies, exact "
                  "utilisation percentages day-by-day, or a document-by-document checklist. DO NOT interrogate "
                  "the promoter on operational minutiae. When such items come up, the RM explicitly says the "
                  "DETAIL will be worked through with their finance/operations colleagues (e.g. 'I'll have my "
                  "team coordinate the document specifics with your finance team, Sir') and DELEGATES it. A "
                  "promoter being walked line-by-line through cheque returns is exactly the WRONG conversation — "
                  "that belongs with the Finance Partner or the Accountant.")
    elif is_finance:
        altitude = "FINANCE_FUNCTION"
        agenda = ("Go into the FINANCIAL detail: utilisation and over-limit days, the cheque returns and what's "
                  "driving them, the credits trend, delayed interest, GST vs banked turnover, and the specific "
                  "documents (GST return, stock statement, insurance, KYC) with timelines. This is the right "
                  "person for the numbers and the document chase. If they are also a partner/director, keep a "
                  "decision-maker's framing but it is appropriate to go deep on the financials with them.")
    elif is_ops:
        altitude = "OPERATIONS_FUNCTION"
        agenda = ("Focus on OPERATIONAL drivers: buyer payment delays, receivables aging, the buyer-concentration "
                  "with the named counterparty, collections, and whether digital collections / POS would help. "
                  "This is the person who can speak to why receipts are slow and how collections actually work — "
                  "not high-level credit policy or the promoter's strategic decisions.")
    else:
        altitude = "SUPPORT_FUNCTION"
        agenda = ("Keep it to the specific items this person owns; be courteous and concise; route anything "
                  "outside their remit to the right colleague.")

    # ---- Indian banking etiquette: address by honorific, never first name
    fn = (stk.get("name") or "").split()[0] if stk.get("name") else ""
    female_markers = ["lakshmi", "priya", "deepa", "anjali", "meena", "kavya", "radha", "sita", "geeta",
                      "anita", "sunita", "pooja", "neha", "divya", "asha", "uma", "rekha", "shanti"]
    honorific = "Madam" if fn.lower() in female_markers else "Sir"

    evidence = {
        "customer": {"name": cust.get("display_name"), "constitution": cust.get("constitution"),
                     "industry": cust.get("industry"), "location": cust.get("city") or cust.get("location"),
                     "risk_grade": cust.get("risk_category"), "rvs": cust.get("relationship_value_score")},
        "speaking_with": {"name": stk["name"], "title": stk["title"], "decision_role": stk["decision_role"],
                          "influence": stk["influence"], "disposition": stk["disposition"],
                          "priorities": stk["priorities"], "concerns": stk["concerns"], "hooks": stk["hooks"],
                          "altitude": altitude, "address_as": honorific},
        "facility": {"type": facility.get("facility_type"), "limit": facility.get("sanction_limit_inr"),
                     "outstanding": facility.get("current_outstanding_inr"), "review_due": facility.get("review_due_date")},
        "conduct": {"credits_trend": f"{conduct['credits_trend_label']} {conduct['credits_trend_pct']}%",
                    "avg_utilization_pct": conduct["avg_utilization_pct"],
                    "peak_utilization_pct": conduct["peak_utilization_pct"],
                    "cheque_returns": conduct["cheque_return_count"],
                    "top_buyer_concentration_pct": conduct.get("top_counterparty_concentration_pct")},
        "early_warning": [{"signal": s["signal_type"], "severity": s["severity"], "evidence": s["evidence_metric"]} for s in ews],
        "enhancement_stance": {"eligible": enh.get("eligible"), "reason": enh.get("reason") or enh.get("rationale")},
        "eligible_offers": [o.get("product") for o in opps],
        "open_service_requests": [{"category": s.get("category"), "priority": s.get("priority")} for s in srs],
        "blocking_documents": [d.get("document_type") for d in docs],
        "receivables_aging": aging, "top_counterparties": tops,
    }

    fallback = _paths_fallback(stk, conduct, srs, docs, honorific)
    if not llm.available():
        return {"customer_id": customer_id, "stakeholder_id": stakeholder_id,
                "stakeholder": evidence["speaking_with"], "altitude": altitude,
                "generated_by": "deterministic_fallback", **fallback}

    schema = (
        '{"paths": [ {'
        '  "path": "happy | neutral | friction",'
        '  "label": "short evocative title for this path",'
        '  "summary": "1 sentence: how this conversation goes and why",'
        '  "turns": [ {"speaker": "RM | ' + (fn or "Customer") + '", "text": "what they say"} ],'
        '  "outcome": "where the conversation lands",'
        '  "rm_technique": "the key RM technique that made this path work (or fail)"'
        '} ] }'
    )
    task = (
        f"Simulate THREE distinct, REALISTIC phone conversations between the RM and {stk['name']} "
        f"({stk['title']}) at {cust.get('display_name')}. The person's disposition is '{stk['disposition']}'.\n\n"
        f"=== WHO YOU ARE TALKING TO — MATCH THE ALTITUDE ({altitude}) ===\n{agenda}\n"
        "This is the single most important instruction: the AGENDA and DEPTH must fit this person's role. A "
        "Managing Partner / Director conversation is strategic and relationship-level; the granular operational "
        "and document chase belongs with the finance or operations contact, not the promoter.\n\n"
        "=== INDIAN BANKING ETIQUETTE (MANDATORY) ===\n"
        f"The RM must address this person respectfully as '{honorific}' (or 'Mr./Ms. <surname>'), NEVER by first "
        "name. Indian RMs do not call a Managing Partner by their first name. Use 'Sir'/'Madam' naturally and "
        "frequently, as a courteous Indian banker would. The persona may address the RM normally.\n\n"
        "=== THE THREE PATHS ===\n"
        "(1) HAPPY — cooperative, goes well; (2) NEUTRAL — businesslike, no strong emotion; (3) FRICTION — the "
        "persona pushes back and the RM must manage it WITHOUT becoming a clerical interrogation (especially for "
        "a senior person — friction at the top is about trust, fairness and the bank's intent, not a line-item "
        "audit).\n\n"
        "=== LENGTH ===\nEach path is a substantial conversation of AT LEAST 16-20 turns — a real, flowing "
        "dialogue with a greeting, the discussion at the right altitude, back-and-forth, and a close.\n\n"
        "=== GROUNDING ===\nUse the customer's REAL numbers from the evidence, but pitch them at the right level: "
        "a promoter hears 'the account has been running close to its limit and credits are down about a third "
        "this year' (headline); a finance person hears the exact utilisation %, the 6 cheque returns, the "
        "document timelines. Talk in PLAIN business language, not banking jargon. The RM stays policy-safe: "
        "recommendations not approvals, clarification not accusation, no enhancement promise where ineligible. "
        "Differentiate the three paths sharply."
    )
    try:
        data = llm.narrate_json(task, evidence, schema, temperature=0.7, max_tokens=6000)
        paths = data.get("paths", [])
        return {"customer_id": customer_id, "stakeholder_id": stakeholder_id,
                "stakeholder": evidence["speaking_with"], "altitude": altitude,
                "generated_by": "llm_grounded", "paths": paths}
    except Exception:
        return {"customer_id": customer_id, "stakeholder_id": stakeholder_id,
                "stakeholder": evidence["speaking_with"], "altitude": altitude,
                "generated_by": "deterministic_fallback", **fallback}


def _paths_fallback(stk, conduct, srs, docs, honorific="Sir") -> dict:
    base = f"utilisation {conduct['avg_utilization_pct']}%, credits {conduct['credits_trend_label']} {conduct['credits_trend_pct']}%"
    return {"paths": [
        {"path": "happy", "label": "Cooperative review", "summary": f"Engages openly.",
         "turns": [{"speaker": "RM", "text": f"Thank you for the time, {honorific}. I wanted to walk through your account ({base})."},
                   {"speaker": stk["name"].split()[0], "text": "Appreciate the heads-up, happy to talk it through."}],
         "outcome": "Agreed next steps.", "rm_technique": "Lead with transparency."},
        {"path": "neutral", "label": "Businesslike", "summary": f"Matter-of-fact.",
         "turns": [{"speaker": "RM", "text": f"A few items on the account need attention, {honorific} ({base})."},
                   {"speaker": stk["name"].split()[0], "text": "Send me the list and I'll look."}],
         "outcome": "Items shared for review.", "rm_technique": "Keep it factual."},
        {"path": "friction", "label": "Pushback", "summary": f"Resists.",
         "turns": [{"speaker": stk["name"].split()[0], "text": "Why is the bank suddenly raising all this?"},
                   {"speaker": "RM", "text": f"Fair question, {honorific} — let me clarify, this is to protect your lines, not cut them."}],
         "outcome": "De-escalated; follow-up set.", "rm_technique": "Acknowledge, reframe, don't accuse."},
    ]}


# ================================================================ 7. BREACH INTELLIGENCE
def breach_intelligence(store: DataStore, customer_id: str) -> dict:
    """AI reasoning layer over the deterministic breach-radar numbers: what the
    trajectory MEANS, the intervention play, and how to talk to the customer."""
    from app.services.breach_radar import BreachRadar
    try:
        radar = BreachRadar(store, customer_id).snapshot()
    except Exception:
        radar = {}
    cust = store.one("customer_master", customer_id=customer_id) or {}
    conduct = AccountConduct(store, customer_id).summary()
    facility = store.one("facilities", customer_id=customer_id) or {}

    evidence = {
        "customer": {"name": cust.get("display_name"), "risk_grade": cust.get("risk_category")},
        "facility": {"type": facility.get("facility_type"), "limit": facility.get("sanction_limit_inr"),
                     "review_due": facility.get("review_due_date")},
        "breach_radar": radar, "conduct": conduct,
    }
    fallback = {
        "verdict": radar.get("band_label") or "See trajectory.",
        "what_it_means": "Utilisation trajectory and covenant states define the breach risk window.",
        "intervention": "Address blocking documents and engage the customer before the review date.",
        "customer_message": "We want to keep your lines healthy ahead of review — let's resolve a few items.",
        "generated_by": "deterministic_fallback", "customer_id": customer_id,
    }
    if not radar or not llm.available():
        return {**radar, "ai": fallback}
    schema = (
        '{"verdict": "one-line plain verdict on where this facility is heading",'
        ' "what_it_means": "2-3 sentences: what the days-to-breach, utilisation slope and covenant states '
        'actually mean for this account — in working-capital terms",'
        ' "drivers": ["the specific factors pushing toward (or away from) a breach"],'
        ' "intervention": "the concrete play the RM/credit should run, and by when",'
        ' "customer_message": "how to raise this with the customer — candid, not alarming, policy-safe"}'
    )
    task = (
        "You are a credit officer reading this MSME facility's breach radar. Explain — grounded ONLY in the "
        "evidence — what the trajectory means (days-to-breach, utilisation slope, drawing-power headroom, "
        "covenant states), the specific drivers, the intervention play with a timeframe tied to the review "
        "date, and how to raise it with the customer candidly without alarming them. India MSME working-capital "
        "framing. Never allege wrongdoing; never promise a credit decision."
    )
    try:
        ai = llm.narrate_json(task, evidence, schema, temperature=0.4, max_tokens=900)
        ai.update({"customer_id": customer_id, "generated_by": "llm_grounded"})
        return {**radar, "ai": ai}
    except Exception:
        return {**radar, "ai": fallback}


# ================================================================ 8. MISSION ACTION WRITE-UP
def mission_action(store: DataStore, customer_id: str, mission_title: str, mission_kind: str = "") -> dict:
    """For a single mission-board item, generate a grounded 'how to action this' play —
    the concrete steps, the exact figures to cite, what to say, and the CRM action to log."""
    cust = store.one("customer_master", customer_id=customer_id) or {}
    conduct = AccountConduct(store, customer_id).summary()
    ews = EWSEngine(store, customer_id).signals()
    facility = store.one("facilities", customer_id=customer_id) or {}
    aging = store.one("aging", customer_id=customer_id) or {}
    srs = [s for s in store.where("service_requests", customer_id=customer_id) if s.get("status") == "Open"]
    docs = [d for d in store.where("documents", customer_id=customer_id)
            if d.get("status") in ("Pending", "Expired", "Overdue") and d.get("required_flag") == "Y"]
    tops = _top_counterparties(store, customer_id, 3)

    evidence = {
        "customer": {"name": cust.get("display_name"), "risk_grade": cust.get("risk_category"),
                     "segment": cust.get("segment")},
        "mission": {"title": mission_title, "kind": mission_kind},
        "facility": {"type": facility.get("facility_type"), "limit": facility.get("sanction_limit_inr"),
                     "review_due": facility.get("review_due_date")},
        "conduct": conduct, "early_warning": [{"signal": s["signal_type"], "evidence": s["evidence_metric"]} for s in ews],
        "open_service_requests": [{"category": s.get("category"), "priority": s.get("priority")} for s in srs],
        "blocking_documents": [d.get("document_type") for d in docs],
        "receivables_aging": aging, "top_counterparties": tops,
    }
    fallback = {
        "steps": ["Review the relevant data", "Engage the customer", "Log the outcome in CRM"],
        "say_this": "Let's work through this together.",
        "figures_to_cite": [], "crm_action": "Create task",
        "generated_by": "deterministic_fallback",
    }
    if not llm.available():
        return {"customer_id": customer_id, "mission": mission_title, **fallback}
    schema = (
        '{"approach": "1-2 sentence approach for THIS mission, grounded in the data",'
        ' "steps": ["concrete, ordered steps the RM takes to action this mission"],'
        ' "figures_to_cite": ["the exact numbers from the data to reference when doing it"],'
        ' "say_this": "a line the RM can actually say to the customer",'
        ' "crm_action": "the CRM artefact to create (task / note / opportunity / document request)"}'
    )
    task = (
        f"The RM picked this mission from today's board: \"{mission_title}\" ({mission_kind}). Tell them exactly "
        "how to action it for THIS customer, grounded ONLY in the evidence. Reference the specific figures "
        "(utilisation, credits trend, cheque returns, the open dispute, the blocking documents, buyer "
        "concentration, review date) that matter to this mission. Be concrete and India-MSME practical. "
        "Policy-safe: recommendations not approvals, clarification not accusation."
    )
    try:
        out = llm.narrate_json(task, evidence, schema, temperature=0.4, max_tokens=700)
        out.update({"customer_id": customer_id, "mission": mission_title, "generated_by": "llm_grounded"})
        return out
    except Exception:
        return {"customer_id": customer_id, "mission": mission_title, **fallback}
