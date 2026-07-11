"""
backend/app/services/retail_reference.py

Single source of truth for the DERIVED retail-banking numbers the live-call
copilot needs but which are not stored as raw columns: SMA classification,
credit-card statement cycle + minimum-due, monthly finance (interest) charge,
the fee schedule, a debt-consolidation illustration, personal-loan
prepayment / foreclosure terms and a retention / rate-review frame.

Design notes
------------
* Deterministic + pure. Every function takes primitives and returns plain
  dicts/numbers so the Tool API evidence pack, the CRM dashboard and the Node
  Video Assist engine all read ONE consistent set of numbers.
* The rupee amounts here (fees, indicative consolidation rate, foreclosure
  charge) are ILLUSTRATIVE but market-realistic for Indian retail banking and
  are documented in docs/sop/. They are clearly labelled "indicative" so the RM
  never quotes them as a final, approved figure — final terms come from the
  sanction letter / MITC / card schedule of charges.
* Kept intentionally free of DataStore / pandas imports to avoid coupling; the
  caller (collateral.build_evidence_pack) passes the values it already computed.
"""
from __future__ import annotations

from datetime import date, timedelta

# --------------------------------------------------------------------------
# Illustrative Contoso Bank retail schedule of charges (documented in SOPs).
# --------------------------------------------------------------------------
GST_PCT = 18.0

# Credit card (CC-CLASSIC, revolving)
CARD_STATEMENT_DAY = 20            # statement generated on the 20th
CARD_PAYMENT_DUE_DAY = 5           # payment due on the 5th of the next month
CARD_MIN_DUE_PCT = 5.0             # 5% of statement outstanding ...
CARD_MIN_DUE_FLOOR_INR = 500.0     # ... subject to a floor
CARD_LATE_FEE_INR = 1300.0         # for statement outstanding > Rs 25,000
CARD_OVERLIMIT_FEE_PCT = 2.5       # of the over-limit amount ...
CARD_OVERLIMIT_FEE_MIN_INR = 600.0  # ... subject to a minimum

# Personal loan
EMI_BOUNCE_FEE_INR = 500.0         # per failed auto-debit / NACH return
PENAL_INTEREST_PCT_PER_MONTH = 2.0  # on the overdue instalment, per month
PL_FORECLOSURE_CHARGE_PCT = 4.0    # of principal outstanding (fixed-rate loan)
PL_PART_PREPAY_FREE_PCT = 25.0     # of principal, per financial year, no charge
PL_PREPAY_MIN_EMIS_PAID = 12       # lock-in before prepayment is allowed

# Debt consolidation / balance-transfer personal loan (service-recovery)
CONSOLIDATION_RATE_PCT = 13.5      # indicative, better than 42% card / 16.5% PL
CONSOLIDATION_TENURE_MONTHS = 36

# Retention / rate review
# Retention / rate review — capped to SOP-17 rate-match authority (high relationship ~1.5%)
PL_RATE_REVIEW_FLOOR_PCT = 15.0    # best-case indicative floor after a review (<=1.5% off 16.5%)


def _with_gst(base: float) -> dict:
    gst = round(base * GST_PCT / 100.0, 2)
    return {"base_inr": round(base, 2), "gst_inr": gst, "total_inr": round(base + gst, 2)}


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28,
                      31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
    return date(y, m, day)


def amortising_emi(principal: float, annual_rate_pct: float, months: int) -> float:
    """Standard reducing-balance EMI."""
    if months <= 0:
        return round(principal, 2)
    r = annual_rate_pct / 1200.0
    if r == 0:
        return round(principal / months, 2)
    f = (1 + r) ** months
    return round(principal * r * f / (f - 1), 2)


def classify_sma(days_past_due: int) -> dict:
    """RBI-style special-mention-account ladder for a term loan."""
    dpd = int(days_past_due or 0)
    if dpd <= 0:
        cls, meaning = "Standard", "no overdue instalment"
    elif dpd <= 30:
        cls, meaning = "SMA-0", "1-30 days past due"
    elif dpd <= 60:
        cls, meaning = "SMA-1", "31-60 days past due"
    elif dpd <= 90:
        cls, meaning = "SMA-2", "61-90 days past due"
    else:
        cls, meaning = "NPA", "over 90 days past due"
    return {
        "class": cls,
        "meaning": meaning,
        "days_past_due": dpd,
        "days_to_npa": max(0, 91 - dpd) if 0 < dpd <= 90 else (0 if dpd > 90 else None),
        "is_default": cls == "NPA",
    }


def card_statement(outstanding: float, limit: float, today: date) -> dict:
    """Current statement cycle, minimum amount due and total due for the card."""
    stmt = date(today.year, today.month, CARD_STATEMENT_DAY)
    if today.day < CARD_STATEMENT_DAY:
        stmt = _add_months(stmt, -1)
    due = _add_months(date(stmt.year, stmt.month, CARD_PAYMENT_DUE_DAY), 1)
    over_limit = max(0.0, outstanding - limit)
    min_due = max(CARD_MIN_DUE_FLOOR_INR, outstanding * CARD_MIN_DUE_PCT / 100.0 + over_limit)
    return {
        "statement_date": stmt.isoformat(),
        "payment_due_date": due.isoformat(),
        "minimum_due_inr": round(min_due, 2),
        "total_due_inr": round(outstanding, 2),
        "over_limit_inr": round(over_limit, 2),
        "min_due_basis": f"{CARD_MIN_DUE_PCT:.0f}% of the outstanding plus the full over-limit amount",
    }


def finance_charge(outstanding: float, apr_pct: float) -> dict:
    """Monthly revolving interest if the balance is carried (not paid in full)."""
    monthly = outstanding * apr_pct / 1200.0
    return {
        "apr_pct": round(apr_pct, 2),
        "monthly_rate_pct": round(apr_pct / 12.0, 3),
        "on_balance_inr": round(outstanding, 2),
        "monthly_interest_inr": round(monthly, 2),
        "annual_interest_inr": round(monthly * 12, 2),
        "note": "Interest is charged on the average daily balance until the full amount is cleared; paying only the minimum keeps this running.",
    }


def fee_schedule(outstanding: float, over_limit: float, emi_inr: float, overdue_emis: int) -> dict:
    """Indicative charges that apply if the customer misses a card or EMI payment."""
    overlimit_base = max(CARD_OVERLIMIT_FEE_MIN_INR, over_limit * CARD_OVERLIMIT_FEE_PCT / 100.0)
    penal_per_month = emi_inr * PENAL_INTEREST_PCT_PER_MONTH / 100.0
    return {
        "card_late_payment": {**_with_gst(CARD_LATE_FEE_INR),
                              "note": "flat fee for a statement outstanding above Rs 25,000 if the minimum due is missed"},
        "card_over_limit": {**_with_gst(overlimit_base),
                            "note": f"{CARD_OVERLIMIT_FEE_PCT:.1f}% of the over-limit amount, minimum Rs {CARD_OVERLIMIT_FEE_MIN_INR:.0f}"},
        "emi_bounce": {**_with_gst(EMI_BOUNCE_FEE_INR),
                       "note": "per failed auto-debit / NACH return on the personal loan"},
        "penal_interest": {"pct_per_month": PENAL_INTEREST_PCT_PER_MONTH,
                           "per_overdue_emi_per_month_inr": round(penal_per_month, 2),
                           "current_overdue_emis": int(overdue_emis or 0),
                           "running_per_month_inr": round(penal_per_month * int(overdue_emis or 0), 2),
                           "note": "charged on each overdue instalment until it is regularised"},
    }


def consolidation(card_outstanding: float, disputed_inr: float, loan_outstanding: float,
                  card_apr_pct: float) -> dict:
    """Illustrate moving the high-cost revolving card balance to a cheaper
    amortising loan. Uses the VERIFIED (undisputed) card balance so a disputed
    amount is never consolidated."""
    verified_card = max(0.0, card_outstanding - max(0.0, disputed_inr))
    emi = amortising_emi(verified_card, CONSOLIDATION_RATE_PCT, CONSOLIDATION_TENURE_MONTHS)
    total_repay = emi * CONSOLIDATION_TENURE_MONTHS
    card_interest_only_monthly = verified_card * card_apr_pct / 1200.0

    combined_principal = verified_card + max(0.0, loan_outstanding)
    combined_emi = amortising_emi(combined_principal, CONSOLIDATION_RATE_PCT, 48)

    return {
        "indicative_rate_pct": CONSOLIDATION_RATE_PCT,
        "tenure_months": CONSOLIDATION_TENURE_MONTHS,
        "verified_card_balance_inr": round(verified_card, 2),
        "consolidated_emi_inr": round(emi, 2),
        "total_repayment_inr": round(total_repay, 2),
        "total_interest_inr": round(total_repay - verified_card, 2),
        "card_interest_only_monthly_inr": round(card_interest_only_monthly, 2),
        "monthly_saving_vs_card_interest_inr": round(card_interest_only_monthly - emi, 2),
        "combined_with_loan_principal_inr": round(combined_principal, 2),
        "combined_emi_48m_inr": round(combined_emi, 2),
        "note": "Indicative only, subject to eligibility, re-KYC, dispute closure and human credit approval; the disputed amount is excluded until resolved.",
    }


def prepayment(loan_outstanding: float, emis_paid: int, overdue_emis: int, emi_inr: float) -> dict:
    """Part-prepayment and foreclosure terms for the fixed-rate personal loan."""
    fc = loan_outstanding * PL_FORECLOSURE_CHARGE_PCT / 100.0
    arrears = emi_inr * int(overdue_emis or 0)
    return {
        "eligible": int(emis_paid or 0) >= PL_PREPAY_MIN_EMIS_PAID,
        "lock_in_emis": PL_PREPAY_MIN_EMIS_PAID,
        "part_prepay_free_pct_per_year": PL_PART_PREPAY_FREE_PCT,
        "part_prepay_free_limit_inr": round(loan_outstanding * PL_PART_PREPAY_FREE_PCT / 100.0, 2),
        "foreclosure_charge_pct": PL_FORECLOSURE_CHARGE_PCT,
        "foreclosure_charge": _with_gst(fc),
        "arrears_to_clear_first_inr": round(arrears, 2),
        "note": "Fixed-rate loan: part-prepayment up to the yearly free limit carries no charge; full foreclosure attracts the charge plus GST. Any arrears must be cleared first.",
    }


def retention(pl_rate_pct: float, tenure_years: float) -> dict:
    """Frame for 'another bank offered me a lower rate' — value + rate review."""
    return {
        "current_pl_rate_pct": round(pl_rate_pct, 2),
        "indicative_review_floor_pct": PL_RATE_REVIEW_FLOOR_PCT,
        "max_indicative_saving_pct": round(max(0.0, pl_rate_pct - PL_RATE_REVIEW_FLOOR_PCT), 2),
        "relationship_years": round(tenure_years, 1),
        "note": "A rate review can be logged but is not guaranteed and depends on conduct; the SMA-1 arrears and re-KYC must be cleared first. Foreclosing to switch also attracts the foreclosure charge.",
    }


def build_reference(*, card_outstanding: float, card_limit: float, card_apr_pct: float,
                    disputed_inr: float, loan_outstanding: float, loan_rate_pct: float,
                    emi_inr: float, emis_paid: int, overdue_emis: int, max_dpd: int,
                    tenure_years: float, today: date) -> dict:
    """Assemble the full derived-reference block attached to the evidence pack."""
    over_limit = max(0.0, card_outstanding - card_limit)
    return {
        "sma": classify_sma(max_dpd),
        "card_statement": card_statement(card_outstanding, card_limit, today),
        "finance_charge": finance_charge(card_outstanding, card_apr_pct),
        "fees": fee_schedule(card_outstanding, over_limit, emi_inr, overdue_emis),
        "consolidation": consolidation(card_outstanding, disputed_inr, loan_outstanding, card_apr_pct),
        "prepayment": prepayment(loan_outstanding, emis_paid, overdue_emis, emi_inr),
        "retention": retention(loan_rate_pct, tenure_years),
    }
