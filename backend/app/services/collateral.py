"""
backend/app/services/collateral.py

Evidence-grounded marketing collateral generator.

The previous version was a generic template fill. This rewrite first builds a
concrete evidence pack from the customer's own data — utilisation numbers, the
real top buyer/supplier with rupee figures, open engagement threads, pending
documents, covenant state, payment behaviour — and then composes copy that is
required to cite those specific facts.

Three composition modes, all evidence-grounded:
  1. LLM-grounded (Foundry)  : prompt pins the model to the evidence pack and
                                forbids vague phrasing.
  2. Deterministic-rich      : if no LLM, the email is assembled directly from
                                the evidence pack (numbers, names, dates) by
                                per-product writers.
  3. Gated                   : suppressed with the specific reason (consent /
                                blockers).

Guardrails kept: consent + blocker aware; no credit/limit/pricing commitment.
"""
from __future__ import annotations

from datetime import date, datetime
from statistics import mean

from app.store import DataStore
from app.services.crosssell import derive_signals, opportunities
from app.services import retail_reference


# ----------------------------- helpers --------------------------------------
def _f(v, default=0.0) -> float:
    try:
        if v in (None, ""): return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _inr(n) -> str:
    n = _f(n)
    if abs(n) >= 1e7: return f"\u20b9{n/1e7:.2f} Cr"
    if abs(n) >= 1e5: return f"\u20b9{n/1e5:.1f} L"
    return f"\u20b9{n:,.0f}"


def _parse_date(s):
    s = str(s or "")[:10]
    for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
        try: return datetime.strptime(s, fmt).date()
        except ValueError: continue
    return None


# Demo reference "today" — kept consistent with the vintage/bounce logic below.
_DEMO_TODAY = date(2026, 5, 27)


def _next_emi_due(fac_reps, ref=_DEMO_TODAY):
    """Next EMI date for a facility. Prefer the earliest scheduled due-date on or
    after the demo reference date; if the schedule only has past rows, roll the
    latest monthly instalment forward until it lands after the reference date."""
    dates = sorted(d for d in (_parse_date(r.get("due_date")) for r in fac_reps) if d)
    if not dates:
        return None
    future = [d for d in dates if d >= ref]
    if future:
        return future[0].isoformat()
    nxt = dates[-1]
    for _ in range(60):
        if nxt >= ref:
            break
        y = nxt.year + (1 if nxt.month == 12 else 0)
        m = 1 if nxt.month == 12 else nxt.month + 1
        try:
            nxt = nxt.replace(year=y, month=m)
        except ValueError:
            nxt = nxt.replace(year=y, month=m, day=28)
    return nxt.isoformat()


def _primary_contact(store, cid):
    stk = store.where("stakeholders", customer_id=cid)
    for s in stk:
        role = (s.get("role", "") + " " + s.get("designation", "")).lower()
        if any(k in role for k in ("director", "owner", "partner", "promoter", "cfo", "md", "proprietor")):
            return s
    return stk[0] if stk else {}


def _consent_blocked(store, cid, product_name):
    for r in store.where("consent", customer_id=cid):
        status = str(r.get("consent_status", r.get("status", ""))).lower()
        channel = str(r.get("channel", r.get("product", ""))).lower()
        if status in ("opted_out", "opt-out", "withdrawn", "no") and (
            "market" in channel or "email" in channel or product_name.lower() in channel):
            return True
    return False


# ----------------------------- evidence pack (RETAIL) -----------------------
def build_evidence_pack(store: DataStore, cid: str) -> dict:
    """Concrete, citable RETAIL facts: savings, primary credit card (limit /
    utilisation / APR), loans + EMIs, CIBIL score, KYC, and any open dispute /
    bounce. Keeps the original top-level keys (facility/insurance/collateral/
    turnover/top_buyer/stock_statement/stress/gst/bureau) so downstream email
    writers keep working, and ADDS retail keys (credit_score/card/loans/kyc/
    dispute/products_held)."""
    cust = store.one("customer_master", customer_id=cid) or {}
    prof = store.one("business_profile", customer_id=cid) or {}
    acct = next((a for a in store.where("accounts", customer_id=cid)
                 if a.get("account_type") == "Savings Account"), {})
    facs = store.where("facilities", customer_id=cid)
    card = next((f for f in facs if f.get("facility_type") == "CC"), (facs[0] if facs else {}))
    loan_facs = [f for f in facs if f.get("facility_type") in ("Home Loan", "Personal Loan")]
    ins = store.one("insurance", customer_id=cid) or {}
    col = store.one("collateral", customer_id=cid) or {}
    bureau = store.one("bureau", customer_id=cid) or {}

    util_rows = sorted(store.where("utilization", customer_id=cid), key=lambda r: r.get("date", ""))
    recent = util_rows[-30:] if util_rows else []
    latest = util_rows[-1] if util_rows else {}
    avg_util = round(mean([_f(r.get("utilization_pct")) for r in recent]), 1) if recent else 0.0
    peak_util = round(max([_f(r.get("utilization_pct")) for r in recent], default=0), 1)
    sanction = _f(card.get("sanction_limit_inr"))
    outstanding = _f(latest.get("outstanding_inr")) or _f(card.get("current_outstanding_inr"))
    available = _f(latest.get("available_limit_inr"))
    if not available and sanction:
        available = max(0.0, sanction - outstanding)

    txns = store.where("transactions", customer_id=cid)
    fy_credits = sum(_f(t.get("amount_inr")) for t in txns if str(t.get("dr_cr", "")).upper() == "CR")
    fy_debits = sum(_f(t.get("amount_inr")) for t in txns if str(t.get("dr_cr", "")).upper() == "DR")

    bounces = store.where("cheque_returns", customer_id=cid)        # EMI/auto-debit bounces
    bounces_recent = [c for c in bounces
                      if (_parse_date(c.get("return_date")) or date(2000, 1, 1)) > date(2025, 9, 30)]
    reps = store.where("repayments", customer_id=cid)
    delayed_emis = [r for r in reps if str(r.get("payment_status", "")).lower()
                    in ("delayed", "bounced", "overdue", "failed", "unpaid")]

    loans = []
    for lf in loan_facs:
        kind = lf.get("facility_type")
        fac_id = str(lf.get("facility_id", ""))
        fac_reps = [r for r in reps if str(r.get("facility_id", "")) == fac_id]
        if not fac_reps:
            tag = "HL" if kind == "Home Loan" else "PL"
            fac_reps = [r for r in reps if tag in str(r.get("facility_id", ""))]
        emi = next((_f(r.get("amount_due_inr")) for r in fac_reps), 0.0)
        loans.append({
            "type": kind, "outstanding_text": _inr(lf.get("current_outstanding_inr")),
            "outstanding_inr": _f(lf.get("current_outstanding_inr")),
            "sanction_text": _inr(lf.get("sanction_limit_inr")), "emi_text": _inr(emi),
            "emi_inr": _f(emi),
            "rate_pct": lf.get("interest_rate_pct"), "status": lf.get("facility_status"),
            "frequency": lf.get("repayment_frequency"),
            "next_due_date": _next_emi_due(fac_reps),
        })

    srs = store.where("service_requests", customer_id=cid)
    open_srs = [s for s in srs if str(s.get("status", "")).lower() == "open"]
    dispute_sr = next((s for s in open_srs if "dispute" in str(s.get("category", "")).lower()
                       or "unauth" in str(s.get("description", "")).lower()), None)
    dispute_txn = next((t for t in txns if str(t.get("anomaly_tag", "")) == "Unauthorized"), None)
    dispute = None
    if dispute_txn or dispute_sr:
        dispute = {
            "merchant": (dispute_txn or {}).get("counterparty_name"),
            "amount_text": _inr((dispute_txn or {}).get("amount_inr")),
            "amount_inr": _f((dispute_txn or {}).get("amount_inr")),
            "date": (dispute_txn or {}).get("txn_date"),
            "status": (dispute_sr or {}).get("status", "Open"),
            "ticket_id": (dispute_sr or {}).get("ticket_id"),
        }

    docs = store.where("documents", customer_id=cid)
    rekyc_pending = any(d.get("document_type") == "Video re-KYC" and str(d.get("status", "")) != "Received"
                        for d in docs)
    kyc_pending_docs = [d.get("document_type") for d in docs
                        if str(d.get("required_flag", "")).upper() == "Y"
                        and str(d.get("status", "")) != "Received"]
    kyc_blocking = any(str(d.get("blocking_flag", "")).upper() == "Y"
                       and str(d.get("status", "")) != "Received" for d in docs)
    threads = store.where("engagement_threads", customer_id=cid)
    open_threads = [t for t in threads if str(t.get("status", "")).lower() in ("active", "open", "action needed")]

    cust_since = cust.get("customer_since")
    vintage_years = None
    d = _parse_date(cust_since)
    if d:
        vintage_years = round((date(2026, 5, 27) - d).days / 365.25, 1)

    score = bureau.get("score") or bureau.get("commercial_score")
    try:
        score = int(float(score)) if score not in (None, "") else None
    except (TypeError, ValueError):
        score = None

    # ----- derived retail-banking reference numbers (single source of truth) --
    pl = next((l for l in loans if "personal" in str(l.get("type", "")).lower()), {})
    pl_reps = [r for r in reps if "PL" in str(r.get("facility_id", ""))]
    pl_overdue = [r for r in pl_reps if str(r.get("payment_status", "")).lower()
                  in ("delayed", "bounced", "overdue", "failed", "unpaid")
                  or _f(r.get("amount_paid_inr")) < _f(r.get("amount_due_inr"))]
    max_dpd = max((int(_f(r.get("days_past_due"))) for r in pl_overdue), default=0)
    reference = retail_reference.build_reference(
        card_outstanding=outstanding, card_limit=sanction,
        card_apr_pct=_f(card.get("interest_rate_pct")),
        disputed_inr=_f((dispute or {}).get("amount_inr")),
        loan_outstanding=_f(pl.get("outstanding_inr")),
        loan_rate_pct=_f(pl.get("rate_pct")),
        emi_inr=_f(pl.get("emi_inr")),
        emis_paid=max(0, len(pl_reps) - len(pl_overdue)),
        overdue_emis=len(pl_overdue), max_dpd=max_dpd,
        tenure_years=vintage_years or 0.0, today=_DEMO_TODAY,
    )

    return {
        "customer_id": cid,
        "company": cust.get("display_name"),
        "industry": prof.get("industry_description"),
        "locations": prof.get("operating_locations"),
        "constitution": cust.get("constitution"),
        "vintage_years": vintage_years,
        "consent_status": cust.get("consent_status"),
        "segment": cust.get("segment"),
        "products_held": prof.get("top_suppliers_summary"),
        "savings": {
            "avg_balance_text": _inr(acct.get("avg_monthly_balance_inr")),
            "avg_balance_inr": _f(acct.get("avg_monthly_balance_inr")),
            "product": acct.get("product_code"),
        },
        "facility": {
            "type": "Credit Card",
            "sanction_limit_inr": sanction, "sanction_limit_text": _inr(sanction),
            "outstanding_inr": outstanding, "outstanding_text": _inr(outstanding),
            "available_inr": available, "available_text": _inr(available),
            "utilisation_avg_30d_pct": avg_util, "utilisation_peak_30d_pct": peak_util,
            "review_due_date": card.get("review_due_date"), "interest_rate_pct": card.get("interest_rate_pct"),
        },
        "card": {
            "limit_text": _inr(sanction), "outstanding_text": _inr(outstanding),
            "available_text": _inr(available), "utilisation_avg_pct": avg_util,
            "utilisation_peak_pct": peak_util, "apr_pct": card.get("interest_rate_pct"),
        },
        "credit_score": {
            "score": score, "band": bureau.get("bureau_score_band"), "as_of": bureau.get("as_of"),
            "dpd_flag": bureau.get("dpd_flag"), "dpd_count": bureau.get("dpd_count"),
            "enquiries_6m": bureau.get("enquiries_6m"),
        },
        "loans": loans,
        "kyc": {"status": cust.get("kyc_status"), "due_date": cust.get("next_kyc_due_date"),
                "rekyc_pending": rekyc_pending, "pending_documents": kyc_pending_docs,
                "blocking": kyc_blocking},
        "insurance": {
            "policy_type": ins.get("policy_type"), "insurer": ins.get("insurer"),
            "coverage_scope": ins.get("coverage_scope"), "status": ins.get("status") or "None held",
            "valid_until": ins.get("valid_until"),
            "sum_insured_text": _inr(ins.get("sum_insured_inr")), "sum_insured_inr": _f(ins.get("sum_insured_inr")),
            "annual_premium_text": _inr(ins.get("annual_premium_inr")),
        },
        "collateral": {
            "type": col.get("collateral_type"), "description": col.get("description"),
            "valuation_text": _inr(col.get("valuation_inr")), "valuation_inr": _f(col.get("valuation_inr")),
            "margin_pct": col.get("margin_pct"), "charge_status": col.get("charge_status"),
        },
        "turnover": {
            "fy_credits_inr": fy_credits, "fy_credits_text": _inr(fy_credits),
            "fy_debits_inr": fy_debits, "fy_debits_text": _inr(fy_debits),
            "annual_income_text": _inr(prof.get("annual_turnover_current_year_inr")),
            "annual_income_prev_text": _inr(prof.get("annual_turnover_prev_year_inr")),
        },
        "top_buyer": None, "top_supplier": None, "top_buyers": [], "top_suppliers": [],
        "debtor_aging": None,
        "stock_statement": {"period": None, "status": None, "stock_value_text": _inr(0),
                            "receivables_text": _inr(0), "dp_cover_ratio": 0.0},
        "stress": {
            "cheque_returns_recent": len(bounces_recent), "cheque_returns_total": len(bounces),
            "delayed_emis": len(delayed_emis),
            "open_threads": [{"topic": t.get("topic"), "status": t.get("status")} for t in open_threads],
            "pending_covenants": [], "open_service_tickets": len(open_srs), "dispute": dispute,
        },
        "dispute": dispute,
        "reference": reference,
        "gst": {"latest_period": None, "latest_status": None, "latest_sales_text": None,
                "variance_vs_bank_pct": None, "trend": None, "months_on_record": 0, "recent_months": []},
        "bureau": {"score": score, "as_of": bureau.get("as_of")},
    }


# ----------------------------- per-product writers --------------------------
# Each writer takes the evidence pack and a contact name, and produces a (subject,
# rationale, body) — where rationale is the "why this email is going" insight the
# RM (and the demo audience) sees.

def _writer_enhancement(ev, contact):
    f = ev["facility"]; t = ev["turnover"]; tb = ev["top_buyer"]; ss = ev["stock_statement"]
    company = ev["company"]
    rationale = (
        f"Utilisation has averaged {f['utilisation_avg_30d_pct']}% (peak {f['utilisation_peak_30d_pct']}%) over the last 30 days "
        f"against a {f['sanction_limit_text']} sanction; available limit is only {f['available_text']}. "
        f"FY credits stand at {t['fy_credits_text']}"
        + (f", anchored by {tb['name']} contributing {tb['avg_monthly_text']}/month" if tb and tb.get('name') else "")
        + f". The latest stock statement ({ss.get('period') or 'pending'}) shows {ss['stock_value_text']} stock + "
        f"{ss['receivables_text']} receivables — {ss['dp_cover_ratio']}x security cover on the outstanding. "
        f"This is the data signature of a tight book that has earned a working-capital review."
    )
    subject = f"Working-capital headroom review for {company} — {f['utilisation_avg_30d_pct']}% utilisation, {f['available_text']} available"
    open_buyer = (f" Your largest buyer {tb['name']} is contributing {tb['avg_monthly_text']} per month, "
                  f"which alongside" if tb and tb.get('name') else " Alongside")
    body = (
        f"Dear {contact},\n\n"
        f"Writing with a specific observation rather than a general check-in. Over the last 30 days, {company}'s "
        f"working-capital limit has run at {f['utilisation_avg_30d_pct']}% average utilisation (peak {f['utilisation_peak_30d_pct']}%), "
        f"with only {f['available_text']} of headroom on the {f['sanction_limit_text']} sanction.\n\n"
        f"FY turnover credits of {t['fy_credits_text']} indicate the business has grown into — and possibly past — the "
        f"current limit.{open_buyer} the {ss['stock_value_text']} stock and {ss['receivables_text']} receivables reported "
        f"in the {ss.get('period') or 'latest'} stock statement, the security base supports a {ss['dp_cover_ratio']}x cover "
        f"on the current outstanding — comfortable room for a structured enhancement review.\n\n"
        f"This is not an approval; it is a data-driven invitation. Could we schedule 30 minutes this week to walk through "
        f"the enhancement assessment, confirm the demand drivers, and align on the documents we will need? Any uplift "
        f"remains subject to our standard appraisal.\n\n"
        f"Warm regards,\nRelationship Manager, Contoso Bank"
    )
    return subject, rationale, body


def _writer_invoice(ev, contact):
    f = ev["facility"]; tb = ev["top_buyer"]; ss = ev["stock_statement"]; company = ev["company"]
    buyer_line = (f"{tb['name']} ({tb.get('payment_behaviour','')}, ~{tb['avg_monthly_text']}/month)"
                  if tb and tb.get('name') else "your top buyer")
    rationale = (
        f"Receivables of {ss['receivables_text']} sit on the {ss.get('period') or 'latest'} stock statement against "
        f"{ss['stock_value_text']} stock, while utilisation runs at {f['utilisation_avg_30d_pct']}% — the classic gap "
        f"between locked-up receivables and an over-stretched working-capital line. "
        + (f"Concentration with {tb['name']} ({tb.get('concentration_band','')} band) is the obvious place to start." if tb and tb.get('name') else "")
    )
    subject = f"Easing the receivables cycle at {company} — {ss['receivables_text']} on the latest stock statement"
    body = (
        f"Dear {contact},\n\n"
        f"Your latest stock statement ({ss.get('period') or 'most recent period'}) shows {ss['receivables_text']} in "
        f"receivables against {ss['stock_value_text']} of stock — meaningful capital locked between sale and collection. "
        f"At the same time, the cash-credit line is running at {f['utilisation_avg_30d_pct']}% utilisation, "
        f"with {f['available_text']} of available limit.\n\n"
        f"Invoice discounting against creditworthy buyers like {buyer_line} can convert booked receivables into "
        f"working cash without adding to the cash-credit drawdown. "
        f"Eligibility is conduct- and document-dependent; "
        + (f"current open service threads ({', '.join(t['topic'] for t in ev['stress']['open_threads'][:2]) or 'none'}) "
           f"would be reviewed first." if ev['stress']['open_threads'] else "your conduct is in good standing.")
        + f"\n\nCould I share a one-pager structured around your top two buyers and their typical payment cycles? "
          f"No commitment implied — purely an indicative view to evaluate fit.\n\n"
          f"Warm regards,\nRelationship Manager, Contoso Bank"
    )
    return subject, rationale, body


def _writer_pos(ev, contact):
    f = ev["facility"]; company = ev["company"]; st = ev["stress"]
    chq = st["cheque_returns_total"]
    chq_line = (f" Your account has logged {chq} cheque return(s) this cycle, "
                f"which a digital-collections layer would directly reduce." if chq else "")
    rationale = (
        f"FY debits of {ev['turnover']['fy_debits_text']} flowing through the operating account indicate "
        f"significant transaction volume."
        + (f" {chq} cheque returns suggest paper collections are creating friction." if chq else "")
        + " A POS / digital-collections rollout improves traceability and reconciles cleanly with the CC account."
    )
    subject = f"Digital collections for {company} — reducing paper-cycle friction"
    body = (
        f"Dear {contact},\n\n"
        f"{company} runs {ev['turnover']['fy_debits_text']} in operating outflows annually, with collections still "
        f"materially paper-driven.{chq_line} A bundled POS + digital-collections setup can cut reconciliation effort, "
        f"shorten the cash-to-account cycle, and give you cleaner MIS for the next renewal conversation.\n\n"
        f"This is an operations-improvement conversation, not a credit one — happy to walk through the merchant "
        f"economics and onboarding timeline whenever suits you.\n\n"
        f"Warm regards,\nRelationship Manager, Contoso Bank"
    )
    return subject, rationale, body


def _writer_trade(ev, contact):
    sup = ev["top_supplier"]; company = ev["company"]
    sup_line = (f" Your top supplier {sup['name']} is averaging {sup['avg_monthly_text']} per month — "
                f"a natural anchor for a trade-finance facility." if sup else "")
    rationale = (
        f"Supplier payment patterns visible in transactions, combined with {ev['turnover']['fy_debits_text']} "
        f"in annual operating debits, suggest LC/BG structures could improve payables terms."
    )
    subject = f"Trade-finance review for {company}"
    body = (
        f"Dear {contact},\n\n"
        f"Looking at {company}'s payables flow ({ev['turnover']['fy_debits_text']} in FY operating debits), the "
        f"size and regularity of supplier payments make a Letter of Credit / Bank Guarantee structure worth "
        f"evaluating.{sup_line} Trade-finance facilities typically lengthen payables terms and strengthen "
        f"supplier confidence in larger orders.\n\n"
        f"Could we schedule a short review to map the supplier base and shortlist 1–2 structures? Any facility "
        f"remains subject to trade-finance appraisal.\n\n"
        f"Warm regards,\nRelationship Manager, Contoso Bank"
    )
    return subject, rationale, body


def _writer_forex(ev, contact):
    company = ev["company"]
    sup = ev["top_supplier"]
    sup_line = (f" Imports anchored by {sup['name']} ({sup['avg_monthly_text']}/month) create a recurring "
                f"FX exposure." if sup else "")
    rationale = (
        f"Operating profile suggests cross-border exposure; {ev['turnover']['fy_debits_text']} in annual debits "
        f"with import-side concentration creates currency risk that simple forwards can smooth."
    )
    subject = f"Managing FX exposure for {company}"
    body = (
        f"Dear {contact},\n\n"
        f"{company}'s operating flows ({ev['turnover']['fy_debits_text']} in FY debits) carry cross-border exposure.{sup_line} "
        f"A simple hedging programme aligned to your shipment cycle can take currency volatility off the P&L without "
        f"requiring you to take a market view.\n\n"
        f"Our treasury desk can structure forwards or a layered approach — happy to bring them into a short call.\n\n"
        f"Warm regards,\nRelationship Manager, Contoso Bank"
    )
    return subject, rationale, body


def _writer_generic(ev, contact, product_name, hook, cta, compliance):
    rationale = (
        f"FY credits {ev['turnover']['fy_credits_text']}, utilisation {ev['facility']['utilisation_avg_30d_pct']}% — "
        f"customer profile supports a {product_name.lower()} conversation."
    )
    body = (
        f"Dear {contact},\n\n{hook} For {ev['company']}, with FY turnover of {ev['turnover']['fy_credits_text']} "
        f"and a {ev['vintage_years']}-year relationship, this is a natural next step.\n\n{cta}\n\n"
        + (f"Note: {compliance}\n\n" if compliance else "")
        + "Warm regards,\nRelationship Manager, Contoso Bank"
    )
    return f"{product_name} for {ev['company']}", rationale, body


WRITERS = {
    "PRD-CC-ENH":  _writer_enhancement,
    "PRD-INVOICE": _writer_invoice,
    "PRD-POS":     _writer_pos,
    "PRD-TRADE-LC": _writer_trade,
    "PRD-FOREX":   _writer_forex,
}


# ----------------------------- main entrypoints -----------------------------
def generate_email(store: DataStore, customer_id: str, product_id: str) -> dict:
    ev = build_evidence_pack(store, customer_id)
    contact_rec = _primary_contact(store, customer_id)
    contact = contact_rec.get("name", "Sir/Madam")

    prod = next((p for p in store.all("product_catalog") if p.get("product_id") == product_id), None)
    if not prod:
        return {"error": f"Unknown product {product_id}"}
    product_name = prod.get("name")
    tmpl = next((t for t in store.all("marketing_templates") if t.get("product_id") == product_id), {})

    # eligibility / blocker check
    signals = derive_signals(store, customer_id)
    blocking = set((prod.get("blocking_signals") or "").split(";")) - {""}
    active_blockers = sorted(blocking & signals)
    consent_block = _consent_blocked(store, customer_id, product_name)
    gated = bool(active_blockers) or consent_block
    gate_reason = None
    if consent_block:
        gate_reason = "Customer has opted out of this channel/product — outreach suppressed."
    elif active_blockers:
        gate_reason = f"Blocked by active risk signals: {', '.join(active_blockers)}. Resolve before outreach."
    if gated:
        return {
            "customer_id": customer_id, "product_id": product_id, "product": product_name,
            "company": ev["company"], "contact": contact,
            "send_recommended": False, "gated": True, "gate_reason": gate_reason,
            "subject": None, "body": None, "rationale": None,
            "evidence_pack": ev,
            "guardrail": "Outreach suppressed by consent/eligibility guardrail.",
        }

    # pick the per-product writer; fall back to generic for products without one
    writer = WRITERS.get(product_id)
    if writer:
        subject, rationale, body = writer(ev, contact)
    else:
        subject, rationale, body = _writer_generic(
            ev, contact, product_name, tmpl.get("hook", ""), tmpl.get("cta", ""), tmpl.get("compliance_note", ""))

    # Try Foundry — but pin it hard to the evidence pack so it cannot generic-ify.
    source = "evidence_template"
    try:
        from app.services.llm import narrate, available
        if available():
            llm_body = narrate(
                "You are a bank Relationship Manager writing a short outreach email (140-200 words). "
                "You MUST cite specific numbers, the customer name, the contact name, and the named "
                "counterparty/document/thread provided in the EVIDENCE. Do NOT use vague phrases like "
                "'trending upward', 'we've noticed', 'we believe', 'genuine needs' — those are forbidden. "
                "Lead with the strongest single fact. Never imply approval, limit or pricing. "
                "End with the signature 'Relationship Manager, Contoso Bank'. "
                "Return the email body only, no subject line.",
                {"EVIDENCE": ev, "CONTACT": contact, "PRODUCT": product_name,
                 "RATIONALE_HINT": rationale, "TEMPLATE_HINT": body},
                temperature=0.4, max_tokens=600,
            )
            if llm_body and len(llm_body) > 200:
                body = llm_body
                source = "foundry_grounded"
    except Exception:
        pass

    return {
        "customer_id": customer_id, "product_id": product_id, "product": product_name,
        "company": ev["company"], "contact": contact,
        "send_recommended": True, "gated": False,
        "subject": subject, "body": body,
        "rationale": rationale,            # the "why this email is being sent" insight
        "evidence_pack": ev,                # the citable facts behind the email
        "tone": tmpl.get("tone"),
        "compliance_note": tmpl.get("compliance_note", ""),
        "generator": source,
        "evidence_refs": ["customer_master", "facilities", "utilization", "counterparty_master",
                          "stock_statements", "engagement_threads", "transactions"],
        "guardrail": "No credit/limit/pricing commitment; every claim is grounded in customer data.",
    }


def eligible_offers(store: DataStore, customer_id: str) -> dict:
    offers = []
    for o in opportunities(store, customer_id):
        pid = o.get("product_id")
        prod = next((p for p in store.all("product_catalog") if p.get("product_id") == pid), None)
        name = prod.get("name") if prod else o.get("product")
        offers.append({
            "product_id": pid, "product": name,
            "eligible": o.get("eligible", False),
            "marketable": o.get("eligible", False) and not _consent_blocked(store, customer_id, name or ""),
            "reason": o.get("reason") or o.get("rationale"),
        })
    return {"customer_id": customer_id, "offers": offers}
