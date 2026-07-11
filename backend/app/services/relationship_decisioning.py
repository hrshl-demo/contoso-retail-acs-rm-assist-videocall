"""Quantitative, SOP-grounded decision intelligence for conventional RM Assist.

This module intentionally separates three layers:
  1. observed customer facts from the synthetic core/CRM datasets;
  2. deterministic calculations and policy-rule matching;
  3. optional Foundry narration performed by ``briefing_story``.

The model is never asked to invent a solution. It receives a validated decision
pack containing calculations, applicable SOP clauses, contradictions and
permitted intervention lanes, and explains that pack at runtime.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
import hashlib
import json
import math
import re
from typing import Any

from app.store import DataStore
from app.services.analytics import AccountConduct, EWSEngine
from app.services.crosssell import opportunities


PROMPT_VERSION = "relationship-decision-v2.1"
CALCULATION_VERSION = "retail-quant-v2.1"
POLICY_INDEX_VERSION = "local-sop-decision-index-v1"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _pct(num: float, den: float) -> float:
    return round((num / den * 100.0), 1) if den else 0.0


def _money(value: Any) -> str:
    n = _num(value)
    if abs(n) >= 10_000_000:
        return f"₹{n/10_000_000:.2f} Cr"
    if abs(n) >= 100_000:
        return f"₹{n/100_000:.2f} L"
    return f"₹{n:,.0f}"


def _parse_date(value: Any) -> date | None:
    text = str(value or "")[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _emi(principal: float, annual_rate_pct: float, months: int) -> float:
    if principal <= 0 or months <= 0:
        return 0.0
    monthly = annual_rate_pct / 1200.0
    if monthly <= 0:
        return principal / months
    power = (1 + monthly) ** months
    return principal * monthly * power / (power - 1)


def _calc(calc_id: str, label: str, formula: str, value: Any, display: str,
          interpretation: str, tone: str = "neutral", sources: list[str] | None = None) -> dict:
    return {
        "calculation_id": calc_id,
        "label": label,
        "formula": formula,
        "value": value,
        "display": display,
        "interpretation": interpretation,
        "tone": tone,
        "sources": sources or [],
    }


def _policy(rule_id: str, title: str, sop_ref: str, clause: str, why: str,
            decision_effect: str, evidence: list[str], priority: str = "High") -> dict:
    return {
        "rule_id": rule_id,
        "title": title,
        "sop_ref": sop_ref,
        "clause": clause,
        "why_applicable": why,
        "decision_effect": decision_effect,
        "evidence": evidence,
        "priority": priority,
    }


def _solution(solution_id: str, title: str, lane: str, description: str,
              steps: list[dict], quantified_outcomes: list[dict], policy_refs: list[str],
              guardrail: str, recommended: bool = False, product_or_service: str = "service") -> dict:
    return {
        "solution_id": solution_id,
        "title": title,
        "lane": lane,
        "description": description,
        "steps": steps,
        "quantified_outcomes": quantified_outcomes,
        "policy_refs": policy_refs,
        "guardrail": guardrail,
        "recommended": recommended,
        "product_or_service": product_or_service,
    }


def _extract_dispute_amount(service_rows: list[dict]) -> tuple[float, dict]:
    for row in service_rows:
        text = " ".join(str(row.get(k, "")) for k in ("category", "description", "remarks"))
        if "dispute" not in text.lower() and "unauthor" not in text.lower():
            continue
        # Prefer an explicit INR/rupee amount, then any 4+ digit number.
        matches = re.findall(r"(?:₹|rs\.?\s*)?([0-9][0-9,]{3,})", text, flags=re.I)
        for raw in matches:
            amount = _num(raw.replace(",", ""))
            if amount >= 1_000:
                return amount, row
    return 0.0, {}


def _score_prior(value: str) -> float:
    m = re.search(r"fell\s+from\s+([0-9]{3})", str(value or ""), flags=re.I)
    return _num(m.group(1)) if m else 0.0


def _record_as_of(customer: dict, transactions: list[dict], bureau: dict) -> date:
    dates = [_parse_date(customer.get("updated_at")), _parse_date(bureau.get("as_of"))]
    dates.extend(_parse_date(t.get("txn_date")) for t in transactions[-20:])
    return max([d for d in dates if d] or [date.today()])


def _playbook_params(store: DataStore) -> dict[str, dict]:
    return {r.get("solution_id", ""): r for r in store.all("solution_playbooks") if r.get("solution_id")}


def build_decision_pack(store: DataStore, customer_id: str) -> dict:
    customer = store.one("customer_master", customer_id=customer_id) or {}
    profile = store.one("business_profile", customer_id=customer_id) or {}
    facilities = store.where("facilities", customer_id=customer_id)
    bureau = store.one("bureau", customer_id=customer_id) or {}
    txns = store.where("transactions", customer_id=customer_id)
    repayments = store.where("repayments", customer_id=customer_id)
    service = [r for r in store.where("service_requests", customer_id=customer_id)
               if str(r.get("status", "")).lower() == "open"]
    documents = store.where("documents", customer_id=customer_id)
    interactions = store.where("interactions", customer_id=customer_id)
    conduct = AccountConduct(store, customer_id).summary()
    ews = EWSEngine(store, customer_id).signals()
    opps = opportunities(store, customer_id)
    params = _playbook_params(store)
    as_of = _record_as_of(customer, txns, bureau)

    monthly_credits = list((conduct.get("monthly_credits") or {}).values())
    first_q = sum(monthly_credits[:3]) / len(monthly_credits[:3]) if monthly_credits[:3] else 0.0
    last_q = sum(monthly_credits[-3:]) / len(monthly_credits[-3:]) if monthly_credits[-3:] else 0.0
    credit_delta = last_q - first_q
    annual_bank_credits = sum(monthly_credits)
    avg_monthly_credit = _num(conduct.get("avg_monthly_credit_inr"))
    turnover_current = _num(profile.get("annual_turnover_current_year_inr"))
    turnover_prev = _num(profile.get("annual_turnover_prev_year_inr"))
    turnover_change_pct = _pct(turnover_current - turnover_prev, turnover_prev)
    bank_capture_pct = _pct(annual_bank_credits, turnover_current)

    category_debits: dict[str, float] = defaultdict(float)
    for t in txns:
        if t.get("dr_cr") == "DR" and t.get("is_return") != "Y":
            category_debits[str(t.get("category_lvl1") or "Other")] += _num(t.get("amount_inr"))
    card_bill_avg = category_debits.get("Card bill", 0.0) / max(1, int(conduct.get("months_covered") or 12))

    card = next((f for f in facilities if str(f.get("facility_type")).upper() in ("CC", "CREDIT CARD")), {})
    personal_loan = next((f for f in facilities if "PERSONAL" in str(f.get("facility_type", "")).upper()), {})
    card_limit = _num(card.get("sanction_limit_inr"))
    card_outstanding = _num(card.get("current_outstanding_inr"))
    card_apr = _num(card.get("interest_rate_pct"))
    card_current_util = _pct(card_outstanding, card_limit)
    pl_outstanding = _num(personal_loan.get("current_outstanding_inr"))
    total_debt = sum(_num(f.get("current_outstanding_inr")) for f in facilities)

    due_values = [_num(r.get("amount_due_inr")) for r in repayments if _num(r.get("amount_due_inr")) > 0]
    monthly_emi = max(due_values) if due_values else 0.0
    delayed = [r for r in repayments if str(r.get("payment_status", "")).lower() in ("delayed", "bounced", "unpaid") or _num(r.get("amount_paid_inr")) < _num(r.get("amount_due_inr"))]
    bounce_count = len(delayed)
    bounce_rate = _pct(bounce_count, len(repayments))
    missed_at_due = sum(max(0.0, _num(r.get("amount_due_inr")) - _num(r.get("amount_paid_inr"))) for r in delayed)
    observed_debt_service = monthly_emi + card_bill_avg
    debt_service_to_credits = _pct(observed_debt_service, avg_monthly_credit)

    dispute_amount, dispute_case = _extract_dispute_amount(service)
    undisputed_card_balance = max(0.0, card_outstanding - dispute_amount)
    undisputed_util = _pct(undisputed_card_balance, card_limit)
    dispute_interest_monthly = dispute_amount * card_apr / 1200.0
    dispute_interest_annual = dispute_amount * card_apr / 100.0

    score = _num(bureau.get("score"))
    prior_score = _score_prior(bureau.get("bureau_score_band") or bureau.get("remarks"))
    score_drop = max(0.0, prior_score - score) if prior_score else 0.0

    open_sla_rows = []
    for row in service:
        due = _parse_date(row.get("sla_due_date"))
        overdue = max(0, (as_of - due).days) if due else 0
        open_sla_rows.append({**row, "overdue_days": overdue})
    max_overdue = max([r["overdue_days"] for r in open_sla_rows] or [0])

    calculations: list[dict] = [
        _calc("CALC-CREDIT-TREND", "Quarterly bank-credit movement",
              "(last 3-month average − first 3-month average) ÷ first 3-month average",
              _num(conduct.get("credits_trend_pct")), f"{_num(conduct.get('credits_trend_pct')):.1f}%",
              f"Average monthly credits moved from {_money(first_q)} to {_money(last_q)}, a {_money(abs(credit_delta))} monthly change.",
              "risk" if credit_delta < 0 else "positive", ["transactions", "account_conduct"]),
        _calc("CALC-TURNOVER", "Declared annual-income movement",
              "(current annual turnover − previous annual turnover) ÷ previous annual turnover",
              turnover_change_pct, f"{turnover_change_pct:+.1f}%",
              f"Declared annual income moved from {_money(turnover_prev)} to {_money(turnover_current)}.",
              "risk" if turnover_change_pct < 0 else "positive", ["business_profile", "financials"]),
        _calc("CALC-CAPTURE", "Bank-credit capture",
              "annual credits observed in bank ÷ declared annual turnover",
              bank_capture_pct, f"{bank_capture_pct:.1f}%",
              f"The bank observed {_money(annual_bank_credits)} of credits against {_money(turnover_current)} declared turnover.",
              "neutral", ["transactions", "business_profile"]),
    ]

    if card:
        calculations.extend([
            _calc("CALC-CARD-UTIL", "Current card utilisation",
                  "current card outstanding ÷ sanctioned card limit",
                  card_current_util, f"{card_current_util:.1f}%",
                  f"{_money(card_outstanding)} is outstanding against a {_money(card_limit)} limit.",
                  "risk" if card_current_util > 80 else "positive", ["loan_facilities"]),
            _calc("CALC-CARD-COST", "Annualised revolving cost",
                  "current outstanding × card APR",
                  card_outstanding * card_apr / 100.0, _money(card_outstanding * card_apr / 100.0),
                  f"At {card_apr:.1f}% APR, a static balance would imply roughly {_money(card_outstanding * card_apr / 1200.0)} interest per month before taxes/fees.",
                  "risk" if card_apr >= 30 else "neutral", ["loan_facilities"]),
        ])
    if dispute_amount and card:
        calculations.extend([
            _calc("CALC-DISPUTE-UTIL", "Utilisation excluding disputed amount",
                  "(card outstanding − disputed amount) ÷ card limit",
                  undisputed_util, f"{undisputed_util:.1f}%",
                  f"Isolating the disputed {_money(dispute_amount)} would move utilisation from {card_current_util:.1f}% to {undisputed_util:.1f}%.",
                  "positive", ["service_requests", "loan_facilities"]),
            _calc("CALC-DISPUTE-COST", "Interest attributable to disputed amount",
                  "disputed amount × card APR ÷ 12",
                  dispute_interest_monthly, f"{_money(dispute_interest_monthly)}/month",
                  f"The disputed leg represents about {_money(dispute_interest_annual)} annualised interest exposure at the current APR while unresolved.",
                  "risk", ["service_requests", "loan_facilities"]),
        ])
    if repayments:
        calculations.extend([
            _calc("CALC-BOUNCE-RATE", "Repayment failure rate",
                  "missed/delayed instalments ÷ scheduled instalments",
                  bounce_rate, f"{bounce_count}/{len(repayments)} · {bounce_rate:.1f}%",
                  f"{_money(missed_at_due)} was not paid on the scheduled due dates across the failed instalments.",
                  "risk" if bounce_count >= 2 else "neutral", ["repayment_history", "cheque_returns"]),
            _calc("CALC-DEBT-SERVICE", "Observed debt-service load",
                  "personal-loan EMI + average monthly card-bill payment, divided by average monthly credits",
                  debt_service_to_credits, f"{debt_service_to_credits:.1f}%",
                  f"Observed monthly debt payments are about {_money(observed_debt_service)} against {_money(avg_monthly_credit)} average monthly credits.",
                  "risk" if debt_service_to_credits > 35 else "neutral", ["repayment_history", "transactions", "account_conduct"]),
        ])
    if prior_score:
        calculations.append(_calc(
            "CALC-SCORE-DROP", "Bureau-score deterioration", "previous score − current score",
            score_drop, f"{int(prior_score)} → {int(score)} · −{int(score_drop)} points",
            f"The score decline coincides with revolving utilisation and recent repayment failures; causality still requires bureau-level review.",
            "risk" if score_drop else "neutral", ["bureau_summary"]))
    if max_overdue:
        calculations.append(_calc(
            "CALC-SLA-AGE", "Oldest open-case SLA breach", "record as-of date − case SLA due date",
            max_overdue, f"{max_overdue} days overdue",
            "The unresolved complaint has exceeded the bank's own recorded SLA and now requires escalation rather than another generic follow-up.",
            "risk", ["service_requests", "customer_master.updated_at"]))

    policy_matches: list[dict] = []
    if dispute_amount:
        policy_matches.extend([
            _policy("POL-DISPUTE-01", "Unauthorised transaction: protect and investigate",
                    "SOP 02 · Card Dispute & Chargeback", "Hot-list the instrument, register the dispute, raise chargeback and assess provisional credit where applicable.",
                    f"An open unauthorised-transaction case for {_money(dispute_amount)} exists.",
                    "The disputed amount must be isolated and its provisional-credit/chargeback status decided before treating the full balance as normal customer debt.",
                    [dispute_case.get("ticket_id", "service_request"), "loan_facilities"]),
            _policy("POL-GRIEVANCE-01", "Overdue grievance escalation",
                    "SOP 07 · Fair Practices & Grievance", "Unresolved complaints should be tracked and escalated through the grievance hierarchy; fee waivers cannot be promised by the RM.",
                    f"The oldest open case is {max_overdue} days beyond its recorded SLA.",
                    "Escalate the dispute/fee review to the authorised service and grievance owner with an explicit decision deadline.",
                    [dispute_case.get("ticket_id", "service_request"), "service_requests"]),
        ])
    if card_current_util > 80:
        policy_matches.append(_policy(
            "POL-CARD-STRESS-01", "Do not increase a revolving/over-utilised card limit",
            "SOP 03 · Loan Eligibility & FOIR", "Do not increase the limit on a revolving or over-utilised card; consider EMI conversion instead.",
            f"Current card utilisation is {card_current_util:.1f}% at {card_apr:.1f}% APR.",
            "Suppress card-limit increase and new cash-out credit; assess a structured conversion of the verified undisputed balance.",
            ["loan_facilities", "daily_limit_utilization", "product_rules:PR-001"]))
    if bounce_count >= 2 or "SMA" in str(personal_loan.get("facility_status", "")):
        policy_matches.append(_policy(
            "POL-COLLECTIONS-01", "Stabilise before new credit",
            "SOP 05 · Collections & Restructuring", "For repeat EMI failures or SMA accounts, consider card EMI conversion or a documented hardship step-down before any new credit.",
            f"There are {bounce_count} failed scheduled repayments and the personal loan is recorded as {personal_loan.get('facility_status') or 'under stress'}.",
            "Run affordability-based restructuring scenarios; do not offer a top-up or higher limit.",
            ["repayment_history", "loan_facilities", "product_rules:PR-003"]))
    if str(customer.get("kyc_status", "")).lower() != "valid":
        policy_matches.append(_policy(
            "POL-KYC-01", "KYC blocks new credit, not service recovery",
            "SOP 01 · KYC / Re-KYC", "Pending video re-KYC blocks new credit and limit increases.",
            f"KYC status is {customer.get('kyc_status')} and video re-KYC is pending.",
            "Complete V-CIP in parallel, but do not use KYC as a reason to delay dispute resolution or permitted service-recovery analysis.",
            ["customer_master", "document_status"]))

    if not policy_matches:
        eligible = [o for o in opps if o.get("eligible")]
        if score >= 750 and _num(conduct.get("avg_utilization_pct")) < 50 and bounce_count == 0:
            policy_matches.append(_policy(
                "POL-GROWTH-01", "Clean-conduct growth review",
                "SOP 03 · Loan Eligibility & FOIR", "A high bureau score, clean repayment and low card utilisation support a human-reviewed credit/card upgrade assessment, subject to current income and KYC.",
                f"Score is {int(score)}, average utilisation is {_num(conduct.get('avg_utilization_pct')):.1f}% and no repayment failures are recorded.",
                "A controlled product review is permissible; no rate, amount or approval may be promised.",
                ["bureau_summary", "account_conduct", "repayment_history"]))
        if eligible:
            policy_matches.append(_policy(
                "POL-SUITABILITY-01", "Need-led product selection",
                "SOP 06/08 · Suitability and Consent", "Only position an eligible product after validating the customer need and consent.",
                f"{len(eligible)} product(s) passed deterministic eligibility rules.",
                "Use AI to rank the best fit, but require the RM to validate purpose before creating an opportunity.",
                ["product_catalog", "product_rules", "consent_registry"], "Medium"))

    contradictions: list[dict] = []
    if dispute_amount and card:
        contradictions.append({
            "title": "The card appears over-limit, but the disputed amount explains the breach",
            "left": f"Reported outstanding: {_money(card_outstanding)} ({card_current_util:.1f}% of limit)",
            "right": f"Excluding disputed {_money(dispute_amount)}: {_money(undisputed_card_balance)} ({undisputed_util:.1f}%)",
            "implication": "Do not treat the entire over-limit position as voluntary customer borrowing until the dispute is resolved.",
            "sources": ["loan_facilities", dispute_case.get("ticket_id", "service_requests")],
        })
    if turnover_change_pct and _num(conduct.get("credits_trend_pct")):
        gap = round(turnover_change_pct - _num(conduct.get("credits_trend_pct")), 1)
        if abs(gap) >= 10:
            contradictions.append({
                "title": "Declared income trend and bank-credit trend diverge",
                "left": f"Declared turnover change: {turnover_change_pct:+.1f}%",
                "right": f"Observed bank-credit trend: {_num(conduct.get('credits_trend_pct')):+.1f}%",
                "implication": "The AI should test seasonality, off-bank collections or timing differences before attributing the change to customer deterioration or growth.",
                "sources": ["business_profile", "transactions"],
            })

    reasoning: list[dict] = []
    for index, pol in enumerate(policy_matches[:5], 1):
        reasoning.append({
            "step": index,
            "observed_fact": pol["why_applicable"],
            "policy_test": f"{pol['sop_ref']}: {pol['clause']}",
            "decision_implication": pol["decision_effect"],
            "evidence": pol["evidence"],
        })

    solutions: list[dict] = []
    if dispute_amount and card:
        conv = params.get("SOL-CARD-CONVERSION", {})
        conversion_rate = _num(conv.get("illustrative_rate_pct"), 18.0)
        terms = [int(x) for x in str(conv.get("tenure_options_months") or "24;36").split(";") if x]
        scenario_outcomes = []
        for months in terms[:2]:
            emi = _emi(undisputed_card_balance, conversion_rate, months)
            scenario_outcomes.append({
                "label": f"{months}-month conversion scenario",
                "value": f"{_money(emi)}/month",
                "detail": f"Illustrative {conversion_rate:.1f}% rate; total finance cost about {_money(emi*months-undisputed_card_balance)}. Final terms require card-system eligibility.",
                "tone": "positive" if emi <= card_bill_avg else "neutral",
            })
        solutions.append(_solution(
            "SOL-DISPUTE-NORMALISE", "Dispute-first balance normalisation", "Protect / Service recovery",
            "Separate the unauthorised amount from the customer's verified debt position, accelerate the chargeback decision and assess provisional credit under the approved dispute process.",
            [
                {"order": 1, "action": f"Escalate provisional-credit eligibility for {_money(dispute_amount)}", "owner": "Case-Dispute RM", "due": "Same business day", "evidence": [dispute_case.get("ticket_id", "service_request"), "SOP 02"]},
                {"order": 2, "action": "Recalculate card dues and fees excluding the disputed leg for decisioning", "owner": "Cards Operations", "due": "Within 24 hours", "evidence": ["loan_facilities", "card statement"]},
                {"order": 3, "action": "Review fee/interest reversals attributable to the unresolved dispute", "owner": "Authorised Service Manager", "due": "With dispute decision", "evidence": ["SOP 07", "late-fee case"]},
            ],
            [
                {"label": "Utilisation if disputed amount is isolated", "value": f"{card_current_util:.1f}% → {undisputed_util:.1f}%", "detail": f"Outstanding would move from {_money(card_outstanding)} to {_money(undisputed_card_balance)}."},
                {"label": "Interest exposure attributable to disputed leg", "value": f"{_money(dispute_interest_monthly)}/month", "detail": f"Illustrative at the recorded {card_apr:.1f}% APR."},
                {"label": "Recorded SLA breach", "value": f"{max_overdue} days", "detail": "Requires authorised escalation rather than another generic customer follow-up."},
            ],
            ["SOP 02", "SOP 04", "SOP 07", "SOP 09"],
            "Provisional credit, fee reversal and chargeback outcomes remain subject to authorised operations/network review.", False, "service"))
        solutions.append(_solution(
            "SOL-CARD-CONVERSION", "Restructure only the undisputed revolving balance", "Stabilise / Product-assisted recovery",
            f"After isolating the dispute, compare structured EMI conversion options on the verified {_money(undisputed_card_balance)} balance instead of leaving it revolving at {card_apr:.1f}% APR.",
            [
                {"order": 1, "action": "Confirm the verified undisputed principal", "owner": "Cards Operations", "due": "After dispute isolation", "evidence": ["card statement", "dispute ledger"]},
                {"order": 2, "action": "Generate system-priced 24- and 36-month conversion offers", "owner": "Cards Product", "due": "Within 24 hours of verified balance", "evidence": ["SOP 05", "PRD-CC-EMI"]},
                {"order": 3, "action": "Select a term only after affordability and customer consent", "owner": "RM + Customer", "due": "Customer meeting", "evidence": ["income cash flow", "consent"]},
            ], scenario_outcomes,
            ["SOP 03", "SOP 05"],
            f"The {conversion_rate:.1f}% rate is a POC scenario parameter, not a customer offer; the card platform must generate actual pricing.", False, "product"))
        relief_pct = _num(params.get("SOL-PL-STEPDOWN", {}).get("temporary_relief_pct"), 25.0)
        reduced_emi = monthly_emi * (1 - relief_pct/100) if monthly_emi else 0.0
        relief = monthly_emi - reduced_emi if monthly_emi else 0.0
        card_36_emi = _emi(undisputed_card_balance, conversion_rate, 36)
        combined_36 = card_36_emi + reduced_emi
        if monthly_emi:
            solutions.append(_solution(
                "SOL-PL-STEPDOWN", "Affordability-based personal-loan step-down", "Stabilise / Hardship review",
                "Use the observed repayment failures and income compression to test a temporary step-down or permitted restructuring, rather than a new loan.",
                [
                    {"order": 1, "action": "Build a verified monthly affordability statement", "owner": "RM + Collections Support", "due": "Within 48 hours", "evidence": ["transactions", "repayment_history"]},
                    {"order": 2, "action": "Submit a hardship step-down scenario to the authorised collections reviewer", "owner": "Collections Credit Officer", "due": "After affordability review", "evidence": ["SOP 05"]},
                ],
                [
                    {"label": "Illustrative temporary EMI", "value": f"{_money(monthly_emi)} → {_money(reduced_emi)}", "detail": f"{relief_pct:.0f}% POC step-down creates {_money(relief)} monthly relief; actual restructuring requires approval."},
                    {"label": "Illustrative combined debt service", "value": f"{_money(observed_debt_service)} → {_money(combined_36)}", "detail": f"Combines the 36-month card scenario with the temporary PL step-down; ratio to average credits falls from {debt_service_to_credits:.1f}% to {_pct(combined_36, avg_monthly_credit):.1f}%."},
                ],
                ["SOP 05"], "No re-aging, deferral or step-down is automatic; documented hardship and authorised approval are required.", False, "product"))
        # The recommended outcome is a coordinated package, not a single task.
        package_steps = [
            {"order": 1, "action": f"Decide provisional-credit eligibility for the disputed {_money(dispute_amount)}", "owner": "Case-Dispute RM + Cards Operations", "due": "Same business day", "evidence": ["SOP 02", "SOP 07", dispute_case.get("ticket_id", "service_request")]},
            {"order": 2, "action": f"Rebase the card obligation to the verified undisputed {_money(undisputed_card_balance)}", "owner": "Cards Operations", "due": "Within 24 hours", "evidence": ["card ledger", "dispute ledger"]},
            {"order": 3, "action": "Generate system-priced 24- and 36-month card-conversion options", "owner": "Cards Product", "due": "After rebasing", "evidence": ["SOP 03", "SOP 05", "PRD-CC-EMI"]},
        ]
        if monthly_emi:
            package_steps.extend([
                {"order": 4, "action": "Run a documented hardship step-down assessment on the personal loan", "owner": "Collections Credit Officer", "due": "Within 48 hours", "evidence": ["SOP 05", "repayment_history", "income cash flow"]},
                {"order": 5, "action": "Select the combined plan that restores positive monthly headroom", "owner": "RM + Authorised Reviewer", "due": "Customer meeting", "evidence": ["affordability statement", "system-priced offers"]},
            ])
        package_steps.extend([
            {"order": len(package_steps)+1, "action": "Complete video re-KYC in parallel only to reopen future-credit eligibility", "owner": "RM + KYC Operations", "due": "Within 7 days", "evidence": ["SOP 01", "DOC-B-VID-04"]},
            {"order": len(package_steps)+2, "action": "Suppress fresh cash-out credit and limit increases until clean conduct is re-established", "owner": "RM + Credit", "due": "Immediate control", "evidence": ["SOP 03", "SOP 05"]},
        ])
        package_outcomes = [
            {"label": "Card utilisation after dispute isolation", "value": f"{card_current_util:.1f}% → {undisputed_util:.1f}%", "detail": f"Removes the disputed {_money(dispute_amount)} from the decision balance, subject to authorised dispute treatment."},
            {"label": "Illustrative monthly debt service", "value": f"{_money(observed_debt_service)} → {_money(combined_36)}", "detail": f"Uses the configured 36-month card scenario and {relief_pct:.0f}% temporary PL step-down; a {_pct(observed_debt_service-combined_36, observed_debt_service):.1f}% reduction."},
            {"label": "Debt service / observed credits", "value": f"{debt_service_to_credits:.1f}% → {_pct(combined_36, avg_monthly_credit):.1f}%", "detail": "Shows the affordability improvement if both authorised remedies are executed."},
            {"label": "Disputed-interest exposure isolated", "value": f"{_money(dispute_interest_monthly)}/month", "detail": f"Illustrative at {card_apr:.1f}% APR while the dispute remains unresolved."},
        ]
        solutions.append(_solution(
            "SOL-INTEGRATED-STABILISATION", "Integrated dispute-and-debt stabilisation package", "Stabilise / Multi-team resolution",
            "Resolve the unauthorised card leg first, restructure only verified debt, test temporary hardship relief and suppress new credit until conduct recovers. This is a coordinated bank outcome rather than a generic follow-up task.",
            package_steps, package_outcomes,
            ["SOP 01", "SOP 02", "SOP 03", "SOP 05", "SOP 07", "SOP 09"],
            "Every financial figure is an illustrative POC scenario until the authorised card, collections and dispute systems produce final decisions and terms.", True, "service+product"))
    else:
        eligible = [o for o in opps if o.get("eligible")]
        if eligible:
            top = eligible[0]
            solutions.append(_solution(
                "SOL-GROWTH-REVIEW", f"Need-led {top.get('product')} review", "Grow / Controlled product review",
                top.get("rationale") or "The deterministic eligibility engine found a customer-fit signal.",
                [
                    {"order": 1, "action": "Validate the customer goal, amount and timing", "owner": "RM", "due": "Customer conversation", "evidence": ["customer need", "consent"]},
                    {"order": 2, "action": f"Run current eligibility for {top.get('product')}", "owner": "Product/Credit system", "due": "After need validation", "evidence": [top.get("sop_ref"), "product_rules"]},
                    {"order": 3, "action": "Compare the proposed product with doing nothing and existing facilities", "owner": "RM", "due": "Before customer acceptance", "evidence": ["suitability", "existing products"]},
                ],
                [
                    {"label": "Bureau score", "value": f"{int(score) if score else '—'}", "detail": "Current bureau score used as one eligibility input."},
                    {"label": "Average card utilisation", "value": f"{_num(conduct.get('avg_utilization_pct')):.1f}%", "detail": "Low utilisation supports capacity but does not prove customer need."},
                    {"label": "Repayment failures", "value": f"{bounce_count}", "detail": "Clean conduct supports a controlled review."},
                ],
                [top.get("sop_ref") or "SOP 03", "SOP 08"],
                "No rate, amount or approval is promised. The customer need and current system eligibility must be validated.", True, "product"))
        else:
            solutions.append(_solution(
                "SOL-MONITOR", "Evidence-led relationship review", "Watch",
                "No product or remediation lane currently dominates. Preserve optionality by resolving stale facts and monitoring the next material event.",
                [{"order": 1, "action": "Refresh only material profile facts", "owner": "RM", "due": "Next review", "evidence": ["customer_master", "interactions"]}],
                [], ["SOP 08"], "Do not manufacture a product need.", True, "service"))

    recommended = next((s for s in solutions if s.get("recommended")), solutions[0] if solutions else {})
    suppressed = []
    for o in opps:
        if not o.get("eligible") or (dispute_amount and o.get("category") not in ("Service Recovery",)):
            suppressed.append({
                "product": o.get("product"),
                "reason": ", ".join(o.get("blocked_by") or []) or "Service recovery and stabilisation take precedence.",
            })

    fingerprint_source = {
        "customer_id": customer_id,
        "as_of": as_of.isoformat(),
        "calculations": [(c["calculation_id"], c["value"]) for c in calculations],
        "policy_rules": [p["rule_id"] for p in policy_matches],
        "solutions": [s["solution_id"] for s in solutions],
    }
    evidence_fingerprint = hashlib.sha256(json.dumps(fingerprint_source, sort_keys=True).encode()).hexdigest()[:16]

    return {
        "customer_id": customer_id,
        "as_of": as_of.isoformat(),
        "calculations": calculations,
        "policy_matches": policy_matches,
        "reasoning_summary": reasoning,
        "contradictions": contradictions,
        "solution_options": solutions,
        "recommended_solution": recommended,
        "suppressed_products": suppressed,
        "decision_metrics": {
            "average_monthly_credit_inr": round(avg_monthly_credit, 2),
            "credit_trend_pct": _num(conduct.get("credits_trend_pct")),
            "turnover_change_pct": turnover_change_pct,
            "card_utilization_pct": card_current_util,
            "undisputed_utilization_pct": undisputed_util,
            "debt_service_to_credits_pct": debt_service_to_credits,
            "bureau_score": score,
            "bureau_score_drop": score_drop,
            "open_case_count": len(service),
            "max_sla_overdue_days": max_overdue,
        },
        "runtime_contract": {
            "prompt_version": PROMPT_VERSION,
            "calculation_version": CALCULATION_VERSION,
            "policy_index_version": POLICY_INDEX_VERSION,
            "customer_tables_scanned": [
                "customer_master", "business_profile", "transactions", "loan_facilities", "repayment_history",
                "bureau_summary", "service_requests", "document_status", "rm_interactions", "product_catalog",
            ],
            "sop_clauses_retrieved": len(policy_matches),
            "calculations_executed": len(calculations),
            "evidence_fingerprint": evidence_fingerprint,
        },
    }
