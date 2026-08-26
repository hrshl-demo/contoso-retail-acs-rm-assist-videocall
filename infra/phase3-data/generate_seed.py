#!/usr/bin/env python3
"""
infra/phase3-data/generate_seed.py â€” deterministic RETAIL seed generator.

Fixed-seed, standard-library only, NO network. Regenerates the whole synthetic
retail pack (31 CSVs under data/csv) plus the AI-enriched CRM case narratives
and a truthful provenance manifest.

Design rules
------------
* DETERMINISTIC. The same --seed and --customers always produce byte-identical
  output. Every random stream is derived from (seed, customer_id, purpose), so
  adding a customer never perturbs an existing one.
* GOLDEN TARGET. With --customers 1 the shape reproduces the committed pack:
  516 transactions, 365 daily_balances, 365 daily_limit_utilization,
  14 repayments, 2 cheque_returns, 7 document_status, 5 service_requests,
  6 rm_interactions, 4 opportunities, 5 engagement_threads,
  30 rm_daily_activity, 2 accounts, 2 loan_facilities, 8 counterparties.
* RAKESH IS PRESERVED. Customer 1 is always Rakesh Sharma (CTB-RTL-002) on the
  'stress' persona, carrying every anchor the demo narrative depends on:
  income 1,440,000 -> 1,120,000 YoY, >= 2 EMI bounces, CIBIL 642, the disputed
  Rs 48,500 GlobalMart card transaction, SMA-1 on the personal loan, and card
  utilisation opening in the 82-92% band.
* CONSISTENT BY CONSTRUCTION. Running balances reconcile to within Rs 1, and
  every foreign key the validator checks is emitted from the same in-memory
  objects that produced the referencing rows.

Usage
-----
  python3 infra/phase3-data/generate_seed.py --out /tmp/pack            # dry run
  python3 infra/phase3-data/generate_seed.py --customers 3 --out /tmp/p3
  python3 infra/phase3-data/generate_seed.py --enrich-only              # narratives + manifest only

`--enrich-only` leaves every CSV untouched and only rewrites
data/knowledge_base/crm_cases_enriched.csv and ai_generation_manifest.json from
whatever pack is on disk. That is how the committed pack gets a truthful
manifest without regenerating (and thereby perturbing) the demo data.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))

DEFAULT_SEED = "contoso-retail-v2"
FY_START = date(2025, 4, 1)
FY_END = date(2026, 3, 31)          # inclusive -> 365 days
FY_LABEL = "FY2025-26"
RM_ID = "RM-2207"
BRANCH = "CTB-PUN-017"

# ---------------------------------------------------------------------------
# Personas
# ---------------------------------------------------------------------------
# `priority_hint` in portfolio_assignments.csv is the DECLARED persona and is
# what validate_seed.py checks each customer against.
PERSONAS = {
    # Declining income, bounced EMIs, revolving card, subprime bureau.
    "stress": {
        "txn_total": 516,
        "income_txns": 28,
        "cibil": 642,
        "cibil_band": "Subprime (fell from 705)",
        "turnover_prev": 1440000,
        "turnover_curr": 1120000,
        "util_open_pct": 82.1,
        "util_close_pct": 102.1,
        "emi_bounces": 2,
        "risk_category": "Medium",
        "relationship_value_score": 41,
        "opening_balance": 38000.0,
        # Total discretionary (non-EMI, non-dispute) spend for the year. Sized so
        # the account closes the year roughly where the committed pack does.
        "discretionary_budget": 907500,
    },
    # Comfortable: income growing, no bounces, card used lightly, prime bureau.
    "stable": {
        "txn_total": 462,
        "income_txns": 24,
        "cibil": 771,
        "cibil_band": "Prime",
        "turnover_prev": 1180000,
        "turnover_curr": 1364000,
        "util_open_pct": 21.0,
        "util_close_pct": 17.5,
        "emi_bounces": 0,
        "risk_category": "Low",
        "relationship_value_score": 78,
        "opening_balance": 96000.0,
        "discretionary_budget": 1010000,
    },
    # Coming back from a bad patch: income recovering, one old bounce, bureau repairing.
    "recovering": {
        "txn_total": 489,
        "income_txns": 26,
        "cibil": 694,
        "cibil_band": "Near-prime (repairing)",
        "turnover_prev": 980000,
        "turnover_curr": 1145000,
        "util_open_pct": 63.0,
        "util_close_pct": 44.0,
        "emi_bounces": 1,
        "risk_category": "Medium",
        "relationship_value_score": 58,
        "opening_balance": 51000.0,
        "discretionary_budget": 905000,
    },
}

# Additional customers beyond Rakesh. Kept short and explicit so the pack stays
# reviewable; --customers is capped at len(EXTRA_CUSTOMERS) + 1.
EXTRA_CUSTOMERS = [
    {"customer_id": "CTB-RTL-003", "name": "Meera Iyer", "persona": "stable", "age": 36,
     "occupation": "Salaried (IT services), stable monthly credit", "city": "Baner, Pune, Maharashtra",
     "since": "2019-02-14", "nominee": "Arjun Iyer", "nominee_age": 39, "pan": "AKQPI2210C"},
    {"customer_id": "CTB-RTL-004", "name": "Imran Qureshi", "persona": "recovering", "age": 44,
     "occupation": "Small trader (auto parts), improving receipts", "city": "Hadapsar, Pune, Maharashtra",
     "since": "2020-08-03", "nominee": "Farida Qureshi", "nominee_age": 41, "pan": "CLMPQ7781R"},
    {"customer_id": "CTB-RTL-005", "name": "Anita Deshmukh", "persona": "stable", "age": 52,
     "occupation": "Salaried (public sector), long tenure", "city": "Aundh, Pune, Maharashtra",
     "since": "2015-06-22", "nominee": "Sanjay Deshmukh", "nominee_age": 55, "pan": "DRQPD3390K"},
]

RAKESH = {
    "customer_id": "CTB-RTL-002", "name": "Rakesh Sharma", "persona": "stress", "age": 41,
    "occupation": "Self-employed (travel agency owner), variable income",
    "city": "Kothrud, Pune, Maharashtra", "since": "2022-11-08",
    "nominee": "Sunita Sharma", "nominee_age": 38, "pan": "BKSPS4467L",
}

# Merchant mix. Weights drive how often each appears; `festive` merchants get an
# extra multiplier during the Oct-Dec festival window.
MERCHANTS = [
    # suffix, name, category_lvl2, weight, low, high, festive
    ("GM", "GlobalMart Online", "Retail purchase", 16, 700, 6200, True),
    ("SHOP", "Reliance Trends", "Retail purchase", 12, 900, 7400, True),
    ("FOOD", "Zomato", "Retail purchase", 18, 240, 2100, False),
    ("FUEL", "Indian Oil Fuel", "Retail purchase", 14, 500, 3400, False),
    ("UTIL", "MSEDCL / Utilities", "Retail purchase", 9, 620, 3100, False),
    ("ATM", "ATM cash withdrawal", "ATM withdrawal", 8, 1000, 9000, False),
]

DOC_TYPES = [
    ("PAN", "PAN card", "KYC", "Y", "Customer"),
    ("AAD", "Aadhaar (masked)", "KYC", "Y", "Customer"),
    ("ADD", "Address proof", "KYC", "Y", "Customer"),
    ("INC", "Income proof / salary slip", "KYC", "Y", "Customer"),
    ("VID", "Video re-KYC", "KYC", "Y", "Customer"),
    ("NOM", "Nominee form", "Account", "N", "Operations"),
    ("FOR", "Form 60/PAN linkage", "KYC", "N", "Operations"),
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def rng_for(seed: str, *parts: str) -> random.Random:
    """A dedicated, reproducible stream per (seed, purpose). Isolating streams
    means adding a customer cannot shift an existing customer's numbers."""
    return random.Random("|".join([seed, *[str(p) for p in parts]]))


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def money(x) -> float:
    return round(float(x) + 0.0, 2)


def festival_weight(d: date) -> float:
    """Indian retail spend seasonality: Diwali/wedding build-up Sep-Nov, a
    December holiday bump, and a slow Feb-Mar quarter-end."""
    return {9: 1.35, 10: 1.9, 11: 1.55, 12: 1.3, 1: 0.85, 2: 0.8, 3: 0.9}.get(d.month, 1.0)


def salary_cluster_weight(d: date) -> float:
    """Spending clusters in the days right after money lands."""
    if d.day in (1, 2, 3, 16, 17):
        return 1.6
    if d.day in (4, 5, 18, 19):
        return 1.25
    if 26 <= d.day <= 31:
        return 0.7          # pre-payday trough
    return 1.0


def allocate(total: int, weights: list[float]) -> list[int]:
    """Distribute exactly `total` items across buckets proportionally to
    `weights`, using largest-remainder so the sum is exact (this is what makes
    the golden row counts reproducible)."""
    wsum = sum(weights) or 1.0
    raw = [total * w / wsum for w in weights]
    out = [int(x) for x in raw]
    rem = total - sum(out)
    order = sorted(range(len(weights)), key=lambda i: (raw[i] - out[i], weights[i], -i), reverse=True)
    for i in range(rem):
        out[order[i % len(order)]] += 1
    return out


def emi_dates(start: date, end: date, day: int = 7):
    """Monthly EMI debit cadence on a fixed day of month."""
    d = date(start.year, start.month, day)
    if d < start:
        d = add_months(d, 1)
    while d <= end:
        yield d
        d = add_months(d, 1)


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + (d.month - 1) + n, 12)
    return date(y, m + 1, min(d.day, 28))


# ---------------------------------------------------------------------------
# Per-customer generation
# ---------------------------------------------------------------------------
def generate_customer(spec: dict, seed: str, index: int) -> dict:
    """Build every table for one customer. Returns {table_key: [rows]}."""
    cid = spec["customer_id"]
    p = PERSONAS[spec["persona"]]
    tag = "B" if cid == RAKESH["customer_id"] else f"C{index}"
    is_rakesh = cid == RAKESH["customer_id"]

    sb_acct = f"ACC-CTB-{cid.split('-')[-1]}-SB1"
    cc_fac = f"FAC-{tag}-CC-001"
    pl_fac = f"FAC-{tag}-PL-001"
    cc_acct = f"ACC-{cc_fac}"

    t = {k: [] for k in (
        "customer_master", "business_profile", "portfolio_assignments", "promoters",
        "stakeholders", "accounts", "counterparties", "transactions", "daily_balances",
        "loan_facilities", "daily_limit_utilization", "repayments", "bureau",
        "financials", "cheque_returns", "consent", "documents", "service_requests",
        "audit_log", "crm_tasks", "engagement_threads", "opportunities", "interactions",
    )}

    # ---------------- master data ----------------
    t["customer_master"].append({
        "customer_id": cid, "legal_name": spec["name"], "display_name": spec["name"],
        "constitution": "Individual", "pan_masked": spec["pan"], "gstin_masked": "",
        "udyam_reg_no": "", "customer_since": spec["since"], "home_branch_code": BRANCH,
        "rm_id": RM_ID, "segment": "Retail - Self-employed" if is_rakesh else "Retail - Individual",
        "risk_category": p["risk_category"],
        "kyc_status": "Due" if spec["persona"] == "stress" else "Valid",
        "next_kyc_due_date": "2026-07-31", "consent_status": "Active",
        "relationship_value_score": p["relationship_value_score"],
        "priority_sector_flag": "N",
        "created_at": "2026-05-01T00:00:00", "updated_at": "2026-05-20T14:30:00",
    })

    risk_notes = {
        "stress": "Self-employed; income volatile; revolving credit-card balance; collections risk.",
        "stable": "Salaried with a steady monthly credit; low utilisation; no arrears on file.",
        "recovering": "Trading receipts improving after a weak year; one historic arrear being cured.",
    }[spec["persona"]]
    growth_notes = {
        "stress": "No upsell - service recovery (card dispute), collections support and retention only.",
        "stable": "Eligible for protection and investment conversations; limit headroom is genuine.",
        "recovering": "Consolidation and rate-review conversations before any new exposure.",
    }[spec["persona"]]
    t["business_profile"].append({
        "customer_id": cid, "industry_code": "OCC-SE" if is_rakesh else "OCC-SAL",
        "industry_description": spec["occupation"],
        "business_vintage_years": max(1, 2026 - int(spec["since"][:4])),
        "registered_address": spec["city"], "operating_locations": "Pune", "employee_count": 0,
        "annual_turnover_prev_year_inr": p["turnover_prev"],
        "annual_turnover_current_year_inr": p["turnover_curr"],
        "seasonality_months": "Variable (travel season Oct-Jan)" if is_rakesh else "Even through the year",
        "top_customers_summary": "Irregular business inflows + occasional salary draw" if is_rakesh
                                 else "Regular monthly salary credit",
        "top_suppliers_summary": "Card spends, personal-loan EMI, fuel, utilities",
        "business_model_notes": risk_notes,
        "risk_notes": ("CIBIL fell to 642; card utilisation ~92%; 3 auto-debit/EMI bounces; disputed card txn."
                       if is_rakesh else
                       f"CIBIL {p['cibil']}; card utilisation ~{p['util_close_pct']:.0f}%; "
                       f"{p['emi_bounces']} EMI bounce(s) on file."),
        "growth_notes": growth_notes,
    })
    t["portfolio_assignments"].append({
        "rm_id": RM_ID, "customer_id": cid,
        "portfolio_segment": "Retail - Self-employed" if is_rakesh else "Retail - Individual",
        "priority_hint": spec["persona"],
        "relationship_value_score": p["relationship_value_score"],
    })

    pers_a, pers_b = f"PERS-{tag}-01", f"PERS-{tag}-02"
    if is_rakesh:
        pers_a, pers_b = "PERS-003", "PERS-004"
    t["promoters"] += [
        {"person_id": pers_a, "customer_id": cid, "role": "Primary holder", "name": spec["name"],
         "age": spec["age"], "shareholding_pct": "100.0",
         "bureau_score_band": f"{p['cibil'] - 22}-{p['cibil'] + 18}",
         "net_worth_band_inr": "8-15 lakh", "kyc_status": "Due" if spec["persona"] == "stress" else "Valid",
         "pep_flag": "N", "mobile_masked": f"XXXXXX{1000 + index * 2 + 3}",
         "email_masked": f"{spec['name'].split()[0].lower()}@example.test"},
        {"person_id": pers_b, "customer_id": cid, "role": "Nominee", "name": spec["nominee"],
         "age": spec["nominee_age"], "shareholding_pct": "0.0", "bureau_score_band": "n/a",
         "net_worth_band_inr": "n/a", "kyc_status": "Valid", "pep_flag": "N",
         "mobile_masked": f"XXXXXX{1000 + index * 2 + 4}",
         "email_masked": f"{spec['nominee'].split()[0].lower()}@example.test"},
    ]

    stk = {
        "stress": ("Resolve the card dispute; ease the EMI pressure; keep his credit score from falling further.",
                   "The unauthorised charge, late fees, collections calls, and whether to switch banks.",
                   "Frustrated / anxious",
                   "Service recovery first - fix the dispute, then offer restructuring; be candid and calming."),
        "stable": ("Protect the household against an income shock; make idle balances work harder.",
                   "Whether the bank only calls when it wants to sell something.",
                   "Receptive / unhurried",
                   "Lead with the balance trend, then protection and investment; never manufacture urgency."),
        "recovering": ("Finish curing the old arrear; bring the borrowing cost down.",
                       "Being judged on a bad year that is already behind them.",
                       "Cautious but engaged",
                       "Acknowledge the recovery explicitly, then consolidation and a rate review."),
    }[spec["persona"]]
    t["stakeholders"] += [
        {"customer_id": cid, "stakeholder_id": f"STK-{tag}-1", "parent_id": "", "name": spec["name"],
         "title": "Primary account holder", "person_ref": pers_a, "priorities": stk[0],
         "concerns": stk[1], "influence": "High", "disposition": stk[2], "decision_role": "Decision maker",
         "hooks": stk[3], "role": "", "designation": "", "influence_level": "",
         "decision_disposition": "", "contact_preference": "", "notes": ""},
        {"customer_id": cid, "stakeholder_id": f"STK-{tag}-2", "parent_id": f"STK-{tag}-1",
         "name": spec["nominee"], "title": "Spouse / nominee", "person_ref": pers_b,
         "priorities": "Household budgeting; reducing the high-interest card balance.",
         "concerns": "Cash-flow strain from EMIs and card interest.", "influence": "Medium",
         "disposition": "Worried but cooperative", "decision_role": "Influencer",
         "hooks": "Bring her into a realistic repayment plan; emphasise stability.",
         "role": "", "designation": "", "influence_level": "", "decision_disposition": "",
         "contact_preference": "", "notes": ""},
    ]

    # ---------------- accounts + facilities ----------------
    card_limit = 300000
    card_outstanding = money(card_limit * p["util_close_pct"] / 100.0)
    pl_sanction, pl_outstanding = 600000, 410000
    t["accounts"] += [
        {"account_id": sb_acct, "customer_id": cid, "account_type": "Savings Account",
         "product_code": "SB-REGULAR", "open_date": spec["since"], "currency": "INR", "status": "Active",
         "sanction_limit_inr": "", "drawing_power_inr": "", "interest_rate_pct": "3.0",
         "review_due_date": "", "last_credit_date": FY_END.isoformat(), "last_debit_date": FY_END.isoformat(),
         "avg_monthly_balance_inr": "", "relationship_role": "Primary savings account"},
        {"account_id": cc_acct, "customer_id": cid, "account_type": "Credit Card",
         "product_code": "CC-CLASSIC", "open_date": spec["since"], "currency": "INR", "status": "Active",
         "sanction_limit_inr": card_limit, "drawing_power_inr": card_limit, "interest_rate_pct": "42.0",
         "review_due_date": "2026-06-30", "last_credit_date": FY_END.isoformat(),
         "last_debit_date": FY_END.isoformat(), "avg_monthly_balance_inr": "",
         "relationship_role": "Primary credit card"},
    ]
    pl_status = {"stress": "2 EMIs bounced - SMA-1", "recovering": "1 historic EMI bounce - cured",
                 "stable": "Active"}[spec["persona"]]
    t["loan_facilities"] += [
        {"facility_id": cc_fac, "customer_id": cid, "facility_type": "CC", "sanction_date": spec["since"],
         "sanction_limit_inr": card_limit, "current_outstanding_inr": int(card_outstanding),
         "drawing_power_inr": card_limit, "security_type": "Unsecured (credit card)",
         "interest_rate_pct": "42.0", "review_due_date": "2026-06-30",
         "repayment_frequency": "Monthly statement", "dp_calculation_basis": "Card limit",
         "risk_weight_band": "POC", "facility_status": "Active"},
        {"facility_id": pl_fac, "customer_id": cid, "facility_type": "Personal Loan",
         "sanction_date": spec["since"], "sanction_limit_inr": pl_sanction,
         "current_outstanding_inr": pl_outstanding, "drawing_power_inr": pl_outstanding,
         "security_type": "Unsecured personal loan", "interest_rate_pct": "16.5", "review_due_date": "",
         "repayment_frequency": "Monthly EMI", "dp_calculation_basis": "Amortising",
         "risk_weight_band": "POC", "facility_status": pl_status},
    ]

    # ---------------- counterparties (8) ----------------
    t["counterparties"].append({
        "counterparty_id": f"CP-{tag}-INC", "customer_id": cid,
        "counterparty_name": "Business inflow / Salary draw" if is_rakesh else "Monthly salary credit",
        "counterparty_type": "Buyer", "relationship_start_date": "2022-05-01", "location": "Maharashtra",
        "concentration_band": "High", "related_party_flag": "N", "avg_monthly_value_inr": 105000,
        "payment_behavior": "Irregular" if is_rakesh else "Regular",
    })
    for suffix, name, _cat, weight, lo, hi, _fest in MERCHANTS:
        t["counterparties"].append({
            "counterparty_id": f"CP-{tag}-{suffix}", "customer_id": cid, "counterparty_name": name,
            "counterparty_type": "Supplier", "relationship_start_date": "2022-05-01",
            "location": "Maharashtra",
            "concentration_band": "Medium" if weight >= 12 else "Low", "related_party_flag": "N",
            "avg_monthly_value_inr": int((lo + hi) / 2 * weight / 4),
            "payment_behavior": "Regular",
        })
    t["counterparties"].append({
        "counterparty_id": f"CP-{tag}-BANK", "customer_id": cid, "counterparty_name": "Contoso Bank",
        "counterparty_type": "Bank", "relationship_start_date": "2022-05-01", "location": "Maharashtra",
        "concentration_band": "Low", "related_party_flag": "N", "avg_monthly_value_inr": 0,
        "payment_behavior": "Regular",
    })

    # ---------------- transactions ----------------
    txns, bounces, repayments = _generate_transactions(spec, p, seed, tag, cid, sb_acct, pl_fac, is_rakesh)
    t["transactions"] = txns
    t["cheque_returns"] = bounces
    t["repayments"] = repayments

    # ---------------- daily balances + utilisation ----------------
    t["daily_balances"] = _daily_balances(txns, cid, sb_acct, p["opening_balance"], seed, cid)
    t["daily_limit_utilization"] = _daily_utilisation(cid, cc_fac, card_limit, p, seed)

    # ---------------- bureau / financials / consent ----------------
    dpd_count = p["emi_bounces"]
    t["bureau"].append({
        "customer_id": cid, "person_id": pers_a, "score": p["cibil"], "as_of": "2026-04-01",
        "bureau_score_band": p["cibil_band"], "enquiries_6m": 5 if spec["persona"] == "stress" else 1,
        "dpd_flag": "Y" if dpd_count else "N", "dpd_count": dpd_count,
        "remarks": ("Score fell on card revolve + 2 EMI bounces; recent enquiries" if is_rakesh else
                    {"stable": "Clean repayment record across all tradelines",
                     "recovering": "Score repairing after a cured arrear; no current DPD",
                     "stress": "Score under pressure from revolving utilisation"}[spec["persona"]]),
        "bureau_id": "", "report_date": "", "score_band": "", "active_tradelines": "",
        "total_outstanding_inr": "", "written_off_flag": "", "settled_flag": "",
    })
    t["financials"].append({
        "customer_id": cid, "fy": FY_LABEL, "turnover_inr": p["turnover_curr"],
        "turnover_prev_inr": p["turnover_prev"], "ebitda_margin_pct": "", "net_profit_inr": "",
        "current_ratio": "", "debt_equity": "",
        "remarks": {"stress": "Income volatile / declining", "stable": "Income steady and growing",
                    "recovering": "Income recovering year-on-year"}[spec["persona"]],
    })
    t["consent"].append({
        "customer_id": cid, "consent_type": "Call recording + analytics", "status": "Active",
        "captured_date": "2025-06-30", "expiry_date": "2027-06-30", "channel": "Branch",
        "consent_id": "", "consent_status": "", "consent_date": "", "purpose": "", "source_system": "",
    })

    # ---------------- documents (7) ----------------
    pending = {"stress": {"INC", "VID"}, "recovering": {"VID"}, "stable": set()}[spec["persona"]]
    for i, (code, dtype, period, req, owner) in enumerate(DOC_TYPES):
        is_pending = code in pending
        t["documents"].append({
            "document_id": f"DOC-{tag}-{code}-{i:02d}", "customer_id": cid, "facility_id": cc_fac,
            "document_type": dtype, "period_covered": period, "required_flag": req,
            "status": "Pending" if is_pending else "Received",
            "received_date": "" if is_pending else "2025-06-01", "expiry_date": "",
            "source_uri": f"blob://synthetic/kyc/{tag}/{dtype.lower().replace(' ', '_')}.pdf",
            "blocking_flag": "Y" if code == "VID" and is_pending else "N", "owner": owner,
            "remarks": f"{dtype} {'pending' if is_pending else 'received'}",
        })

    # ---------------- CRM: service requests, tasks, threads, opportunities, interactions ----------------
    _crm_tables(t, spec, p, tag, cid, is_rakesh)

    t["audit_log"].append({
        "audit_id": f"AUD-{tag}-001", "customer_id": cid, "object_type": "seed",
        "action": "data_generated", "actor": "System", "timestamp": "2026-05-24T00:00:00",
        "prompt_version": "n/a", "model_version": "n/a",
        "evidence_refs": f"deterministic generator {DEFAULT_SEED}",
    })
    return t


def _generate_transactions(spec, p, seed, tag, cid, acct, pl_fac, is_rakesh):
    """Chronological transaction ledger with an exact row budget.

    Realism levers, all deterministic:
      * income lands in salary-date clusters rather than uniformly,
      * discretionary spend follows those credits and spikes in the festival window,
      * the EMI debits sit on a fixed monthly cadence,
      * balances drift down (stress) or up (stable/recovering) across the year.
    """
    r = rng_for(seed, cid, "txns")
    days = list(daterange(FY_START, FY_END))
    total = p["txn_total"]
    n_bounces = p["emi_bounces"]

    # --- fixed-cadence rows -------------------------------------------------
    emis = list(emi_dates(FY_START, FY_END, day=7))
    interest_days = [date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31)]
    dispute_day = date(2026, 2, 19) if is_rakesh else None
    fixed_count = len(emis) + len(interest_days) + (1 if dispute_day else 0) + n_bounces

    # --- income: salary-date clusters --------------------------------------
    income_n = p["income_txns"]
    income_days = []
    for m in range(12):
        anchor = add_months(FY_START, m)
        per_month = income_n // 12 + (1 if m < income_n % 12 else 0)
        for k in range(per_month):
            base = 1 if k % 2 == 0 else 16
            jitter = r.randint(0, 3) if is_rakesh else r.randint(0, 1)   # self-employed = less punctual
            try:
                income_days.append(date(anchor.year, anchor.month, min(28, base + jitter)))
            except ValueError:
                income_days.append(anchor)
    income_days.sort()

    # --- discretionary spend: exact remaining budget, weighted by day -------
    spend_n = total - fixed_count - len(income_days)
    if spend_n < 0:
        raise SystemExit(f"txn_total {total} too small for persona {spec['persona']}")
    weights = [festival_weight(d) * salary_cluster_weight(d) * (0.6 if d.weekday() == 6 else 1.0) for d in days]
    per_day = allocate(spend_n, weights)

    merch_weights = [m[3] for m in MERCHANTS]

    # Assemble a per-day bucket of (hint) rows, then walk chronologically so the
    # running balance and the txn ids stay in the order the validator sorts by.
    buckets: dict[date, list[dict]] = {d: [] for d in days}
    for d, n in zip(days, per_day):
        fw = festival_weight(d)
        for _ in range(n):
            m = r.choices(MERCHANTS, weights=merch_weights, k=1)[0]
            suffix, name, cat2, _w, lo, hi, festive = m
            amt = r.uniform(lo, hi) * (1.25 if (festive and fw > 1.2) else 1.0)
            buckets[d].append({
                "kind": "spend", "cp": f"CP-{tag}-{suffix}", "name": name,
                "channel": "ATM" if suffix == "ATM" else "Card",
                "cat1": "Cash" if suffix == "ATM" else "Card spend", "cat2": cat2,
                "amount": money(amt), "conf": round(r.uniform(0.88, 0.99), 2),
            })
    inc_base = p["turnover_curr"] / max(1, len(income_days))
    for d in income_days:
        drift = 1.0 + (0.22 if not is_rakesh else -0.18) * ((d - FY_START).days / 364.0)
        amt = inc_base * drift * r.uniform(0.82, 1.18)
        buckets[d].append({
            "kind": "income", "cp": f"CP-{tag}-INC",
            "name": "Business inflow / Salary draw" if is_rakesh else "Monthly salary credit",
            "channel": "NEFT", "cat1": "Income", "cat2": "Salary credit",
            "amount": money(amt), "conf": round(r.uniform(0.90, 0.97), 2),
        })
    # Rescale discretionary spend onto the persona's annual budget. The shape
    # (clustering, festival spikes, merchant mix) is preserved; only the level
    # moves, so the year closes on a plausible balance instead of drifting.
    spend_items = [it for day in buckets.values() for it in day if it["kind"] == "spend"]
    current = sum(it["amount"] for it in spend_items)
    if current > 0:
        scale = p["discretionary_budget"] / current
        for it in spend_items:
            it["amount"] = money(max(50.0, it["amount"] * scale))
    for d in emis:
        buckets[d].append({
            "kind": "emi", "cp": f"CP-{tag}-BANK", "name": "Contoso Bank (Loan EMI)", "channel": "AutoDebit",
            "cat1": "EMI", "cat2": "Loan EMI", "amount": 18900.0, "conf": 0.97,
        })
    for d in interest_days:
        buckets[d].append({
            "kind": "interest", "cp": f"CP-{tag}-BANK", "name": "Contoso Bank", "channel": "Internal",
            "cat1": "Income", "cat2": "Savings interest",
            "amount": money(r.uniform(900, 1700)), "conf": 0.93,
        })
    if dispute_day:
        buckets[dispute_day].append({
            "kind": "dispute", "cp": f"CP-{tag}-GM", "name": "GlobalMart Online", "channel": "Card",
            "cat1": "Card spend", "cat2": "Disputed transaction", "amount": 48500.0, "conf": 0.92,
        })

    # --- walk the calendar --------------------------------------------------
    rows, seq, balance = [], 0, p["opening_balance"]
    DAY_START, DAY_END = 9 * 3600, 21 * 3600 + 1800
    for d in days:
        items = buckets[d]
        if not items:
            continue
        # Credits settle before the day's spend; the tie-break keeps the order
        # stable for a given seed.
        for it in items:
            it["_rank"] = (0 if it["kind"] in ("income", "interest") else 1, r.random())
        items.sort(key=lambda x: x["_rank"])
        # Timestamps are then assigned MONOTONICALLY across the day so the
        # validator's (txn_timestamp, txn_id) ordering matches the order the
        # running balance was computed in. Jitter is bounded to a third of a
        # slot, so it can never reorder two transactions.
        slot = max(60, (DAY_END - DAY_START) // len(items))
        for j, it in enumerate(items):
            base = DAY_START + slot * j + slot // 2
            it["_secs"] = base + r.randint(-slot // 3, slot // 3)
        for it in items:
            seq += 1
            secs = it["_secs"]
            ts = f"{d.isoformat()}T{secs // 3600:02d}:{(secs % 3600) // 60:02d}:{secs % 60:02d}"
            credit = it["kind"] in ("income", "interest")
            amt = it["amount"]
            balance = money(balance + (amt if credit else -amt))
            disputed = it["kind"] == "dispute"
            rows.append({
                "txn_id": f"TXN-{tag}-{d.strftime('%Y%m%d')}-{seq:04d}", "account_id": acct,
                "customer_id": cid, "txn_date": d.isoformat(), "value_date": d.isoformat(),
                "txn_timestamp": ts, "dr_cr": "CR" if credit else "DR", "amount_inr": amt,
                "balance_after_txn_inr": balance, "channel": it["channel"], "instrument_no": "",
                "description": (f"{it['channel']} {'CR' if credit else 'DR'} {it['name']}"
                                + (" \u2014 DISPUTED (unauthorised)" if disputed else "")),
                "counterparty_id": it["cp"], "counterparty_name": it["name"],
                "counterparty_type": ("Bank" if it["cp"].endswith("-BANK")
                                      else "Buyer" if it["cp"].endswith("-INC") else "Supplier"),
                "category_lvl1": it["cat1"], "category_lvl2": it["cat2"], "gst_invoice_flag": "N",
                "gst_invoice_no": "", "invoice_due_date": "",
                "is_cash": "Y" if it["cat2"] == "ATM withdrawal" else "N", "is_related_party": "N",
                "is_return": "N", "return_reason": "NA",
                "loan_facility_id": pl_fac if it["kind"] == "emi" else "",
                "source_system": "CBS_SYNTH", "classification_confidence": it["conf"],
                "anomaly_tag": "Unauthorized" if disputed else "None",
                "notes": "Customer disputes this card transaction; chargeback raised." if disputed else "",
                "_kind": it["kind"], "_date": d,
            })

    # --- bounced EMIs: the month(s) after the FY window ----------------------
    bounce_rows, bounce_txns = [], []
    for i in range(n_bounces):
        bd = add_months(date(2026, 4, 7), i)
        seq += 1
        txn_id = f"TXN-{tag}-{bd.strftime('%Y%m%d')}-{seq:04d}"
        # A returned debit never moves money, so the balance is carried forward.
        bounce_txns.append({
            "txn_id": txn_id, "account_id": acct, "customer_id": cid, "txn_date": bd.isoformat(),
            "value_date": bd.isoformat(), "txn_timestamp": f"{bd.isoformat()}T09:30:00", "dr_cr": "DR",
            "amount_inr": 18900.0, "balance_after_txn_inr": balance, "channel": "AutoDebit",
            "instrument_no": f"NACH{bd.month}{seq:05d}",
            "description": "AutoDebit DR Loan EMI RETURNED (insufficient balance)",
            "counterparty_id": f"CP-{tag}-BANK", "counterparty_name": "Contoso Bank (Loan EMI)",
            "counterparty_type": "Bank", "category_lvl1": "EMI", "category_lvl2": "Loan EMI",
            "gst_invoice_flag": "N", "gst_invoice_no": "", "invoice_due_date": "", "is_cash": "N",
            "is_related_party": "N", "is_return": "Y",
            "return_reason": "Insufficient balance (auto-debit)", "loan_facility_id": pl_fac,
            "source_system": "CBS_SYNTH", "classification_confidence": 0.97, "anomaly_tag": "EMIBounce",
            "notes": "", "_kind": "bounce", "_date": bd,
        })
        dpd = 50 - i * 30
        bounce_rows.append({
            "return_id": f"BNC-{tag}-{bd.strftime('%Y%m')}-01", "customer_id": cid, "account_id": acct,
            "txn_id": txn_id, "instrument_no": f"NACH{bd.month}{seq:05d}",
            "return_date": bd.isoformat(), "amount_inr": 18900.0,
            "return_reason": "Insufficient balance (auto-debit)", "severity": "High",
            "counterparty_name": "Loan EMI auto-debit",
            "remarks": (f"EMI auto-debit bounce; instalment unpaid \u2014 oldest arrear ~{dpd} dpd (SMA-1)."
                        if i == 0 else f"EMI auto-debit bounce; instalment unpaid \u2014 ~{dpd} dpd."),
        })
    rows += bounce_txns

    # --- repayment history mirrors the EMI ledger ---------------------------
    repayments = []
    emi_txn_by_date = {t["_date"]: t["txn_id"] for t in rows if t["_kind"] == "emi"}
    for d in emis:
        repayments.append({
            "repayment_id": f"RPY-{tag}-{d.strftime('%Y%m')}", "facility_id": pl_fac, "customer_id": cid,
            "due_date": d.isoformat(), "amount_due_inr": 18900.0, "amount_paid_inr": 18900.0,
            "payment_date": d.isoformat(), "days_past_due": 0, "payment_status": "Paid",
            "source_txn_id": emi_txn_by_date.get(d, ""), "remarks": "EMI paid on time",
        })
    for i, br in enumerate(bounce_rows):
        dpd = 50 - i * 30
        repayments.append({
            "repayment_id": f"RPY-{tag}-{br['return_date'][:4]}{br['return_date'][5:7]}",
            "facility_id": pl_fac, "customer_id": cid, "due_date": br["return_date"],
            "amount_due_inr": 18900.0, "amount_paid_inr": 0.0, "payment_date": "",
            "days_past_due": dpd, "payment_status": "Bounced", "source_txn_id": br["txn_id"],
            "remarks": (f"EMI auto-debit bounced (insufficient balance); instalment unpaid \u2014 {dpd} dpd, SMA-1"
                        if i == 0 else
                        f"EMI auto-debit bounced (insufficient balance); instalment unpaid \u2014 {dpd} dpd"),
        })

    for t in rows:
        t.pop("_kind", None)
        t.pop("_date", None)
    return rows, bounce_rows, repayments


def _daily_balances(txns, cid, acct, opening, seed, key):
    r = rng_for(seed, key, "balances")
    by_day: dict[str, list[dict]] = {}
    for t in txns:
        by_day.setdefault(t["txn_date"], []).append(t)
    rows, running = [], money(opening)
    for d in daterange(FY_START, FY_END):
        iso = d.isoformat()
        day_rows = by_day.get(iso, [])
        credits = money(sum(float(t["amount_inr"]) for t in day_rows
                            if t["dr_cr"] == "CR" and t["is_return"] != "Y"))
        debits = money(sum(float(t["amount_inr"]) for t in day_rows
                           if t["dr_cr"] == "DR" and t["is_return"] != "Y"))
        open_bal = running
        closing = money(running + credits - debits)
        intraday = [open_bal] + [float(t["balance_after_txn_inr"]) for t in day_rows] + [closing]
        spread = max(1200.0, abs(closing) * 0.04)
        rows.append({
            "date": iso, "account_id": acct, "customer_id": cid,
            "opening_balance_inr": open_bal,
            "total_credits_inr": credits if day_rows else 0,
            "total_debits_inr": debits if day_rows else 0,
            "closing_balance_inr": closing,
            "min_balance_inr": money(min(intraday) - r.uniform(0, spread)),
            "max_balance_inr": money(max(intraday) + r.uniform(0, spread)),
            "cash_credit_count": sum(1 for t in day_rows if t["is_cash"] == "Y"),
            "cheque_return_count": sum(1 for t in day_rows if t["is_return"] == "Y"),
            "overdrawn_flag": "Y" if closing < 0 else "N",
            "month_end_flag": "Y" if (d + timedelta(days=1)).day == 1 else "N",
        })
        running = closing
    return rows


def _daily_utilisation(cid, fac, limit, p, seed):
    """Card outstanding walks from the persona's opening utilisation to its
    closing utilisation with day-to-day noise, so the trend is legible without
    being a straight line."""
    r = rng_for(seed, cid, "util")
    days = list(daterange(FY_START, FY_END))
    n = len(days)
    # Rakesh's opening band is a hard demo invariant, so his endpoints are fixed.
    # Other customers get a small per-customer offset so two customers on the
    # same persona don't trace identical curves.
    off = 0.0 if cid == RAKESH["customer_id"] else r.uniform(-3.5, 3.5)
    start_out = limit * (p["util_open_pct"] + off) / 100.0
    end_out = limit * (p["util_close_pct"] + off * 0.6) / 100.0
    rows, over_hist = [], []
    for i, d in enumerate(days):
        k = i / max(1, n - 1)
        # ease-in-out so the move is gradual at both ends
        eased = k * k * (3 - 2 * k)
        out = start_out + (end_out - start_out) * eased
        # The endpoints are anchored exactly (the persona's opening band is a
        # hard invariant); only the interior carries day-to-day noise.
        if 0 < i < n - 1:
            out += r.uniform(-0.012, 0.012) * limit * festival_weight(d)
        out = max(0.0, min(limit * 1.15, out))
        util = round(out / limit * 100.0, 1)
        over_hist.append(1 if util >= 85 else 0)
        rows.append({
            "date": d.isoformat(), "facility_id": fac, "customer_id": cid, "sanction_limit_inr": limit,
            "drawing_power_inr": limit, "outstanding_inr": money(out),
            "available_limit_inr": money(limit - out), "utilization_pct": util,
            "over_limit_flag": "Y" if out > limit else "N",
            "days_over_85_pct_rolling_30": sum(over_hist[-30:]),
            "interest_serviced_flag": "Y" if i == n - 1 else "",
            "utilization_reason_tag": {"stress": "Revolving / stress", "stable": "Convenience use",
                                       "recovering": "Paying down"}[
                next(k2 for k2, v in PERSONAS.items() if v is p)],
            "dp_shortfall_inr": "", "source_system": "",
        })
    return rows


def _crm_tables(t, spec, p, tag, cid, is_rakesh):
    """Service requests (5), tasks (4), threads (5), opportunities (4),
    interactions (6) â€” the case surface the RM cockpit and the enrichment layer
    both read from."""
    persona = spec["persona"]
    name = spec["name"]

    sr_defs = {
        "stress": [
            ("2026-02-20", "Card transaction dispute (unauthorised)",
             "Customer disputes 48500 at GlobalMart Online on 2026-02-19; chargeback under review.",
             "Open", "High", "Negative"),
            ("2026-02-20", "EMI bounce / collections call",
             "Two personal-loan EMIs bounced; customer requests restructuring / moratorium.",
             "Open", "High", "Concerned"),
            ("2026-02-20", "Late-payment charge dispute",
             "Customer disputes late-payment fee on the credit card.", "Open", "Medium", "Negative"),
            ("2026-01-15", "Card blocked - reissue", "Customer raised card blocked - reissue",
             "Closed", "Medium", "Neutral"),
            ("2026-01-15", "Statement request", "Customer raised statement request",
             "Closed", "Low", "Neutral"),
        ],
        "stable": [
            ("2026-02-11", "Standing instruction setup",
             "Customer asked to automate the card payment from the savings account.",
             "Closed", "Low", "Positive"),
            ("2026-02-18", "Nominee update", "Customer updated the nominee on the savings account.",
             "Closed", "Low", "Neutral"),
            ("2026-03-02", "Term deposit rate query",
             "Customer asked what rate applies to a 400-day deposit.", "Open", "Low", "Positive"),
            ("2026-01-09", "Statement request", "Customer raised statement request",
             "Closed", "Low", "Neutral"),
            ("2026-03-14", "Insurance renewal reminder",
             "Customer asked when the protection policy renews.", "Open", "Medium", "Neutral"),
        ],
        "recovering": [
            ("2026-01-22", "Arrear clearance confirmation",
             "Customer asked for written confirmation that the historic arrear is cured.",
             "Closed", "High", "Concerned"),
            ("2026-02-14", "Interest rate review request",
             "Customer requested a rate review now that repayments are regular.",
             "Open", "Medium", "Neutral"),
            ("2026-02-27", "Card limit restoration query",
             "Customer asked when the reduced card limit can be restored.", "Open", "Medium", "Concerned"),
            ("2026-01-08", "Statement request", "Customer raised statement request",
             "Closed", "Low", "Neutral"),
            ("2026-03-05", "Consolidation options",
             "Customer asked whether the card balance can move into the personal loan.",
             "Open", "Medium", "Positive"),
        ],
    }[persona]
    for i, (created, cat, desc, status, prio, sent) in enumerate(sr_defs, start=1):
        t["service_requests"].append({
            "ticket_id": f"SR-{tag}-{created[:4]}{created[5:7]}-{i:03d}", "customer_id": cid,
            "created_date": created, "category": cat, "description": desc, "status": status,
            "priority": prio, "sla_due_date": "2026-02-25",
            "closed_date": "2026-01-20" if status == "Closed" else "",
            "customer_sentiment": sent,
            "remarks": "Resolved" if status == "Closed" else "Raise on next call",
        })

    task_defs = {
        "stress": [("Resolve card dispute SR-{sr}-001 (chargeback)", "2026-04-09", "High"),
                   ("Offer EMI restructuring / step-down", "2026-04-10", "High"),
                   ("Compute EMI-conversion savings on card", "2026-04-11", "Medium"),
                   ("Late-fee waiver review + retention call", "2026-04-14", "High")],
        "stable": [("Book the protection review conversation", "2026-04-09", "Medium"),
                   ("Model the surplus-balance deposit ladder", "2026-04-10", "Medium"),
                   ("Confirm the nominee update landed in CBS", "2026-04-11", "Low"),
                   ("Share the 400-day deposit rate card", "2026-04-14", "Low")],
        "recovering": [("Issue the arrear-cured confirmation letter", "2026-04-09", "High"),
                       ("Prepare the rate-review submission", "2026-04-10", "Medium"),
                       ("Model card-to-loan consolidation savings", "2026-04-11", "Medium"),
                       ("Review card limit restoration eligibility", "2026-04-14", "Medium")],
    }[persona]
    for i, (title, due, prio) in enumerate(task_defs, start=1):
        t["crm_tasks"].append({
            "task_id": f"TASK-{tag}-{i:03d}", "customer_id": cid, "rm_id": RM_ID,
            "title": title.replace("{sr}", tag), "due_date": due, "status": "Open",
            "priority": prio, "created_by": "RM", "approval_state": "Approved",
        })

    thread_defs = {
        "stress": [
            ("Card dispute / chargeback (unauthorised txn)", "Action needed", "Critical",
             "Resolve the unauthorised Rs 48,500 transaction first - hot-list, chargeback, provisional credit.",
             "", "disputed card txn; open ticket"),
            ("EMI bounces / collections (SMA-1)", "Active", "High",
             "Two EMIs bounced; offer restructuring / step-down over new credit.",
             "PRD-PL-RESTRUCT", "2 EMI bounces; SMA-1"),
            ("Card EMI conversion (cut 42% APR)", "Open", "High",
             "Convert the revolving balance to EMI to reduce interest and stabilise.",
             "PRD-CC-EMI", "~92% utilisation; 42% APR"),
            ("Late-fee dispute", "Action needed", "Medium",
             "Review the disputed late fee; waiver as service recovery if eligible.", "", "disputed late fee"),
            ("Retention / attrition risk", "Watch", "High",
             "Customer threatening to close the card; front-foot retention + escalation.",
             "", "low relationship value; closure threat"),
        ],
        "stable": [
            ("Protection gap review", "Open", "High",
             "Household has no term cover against the single salary; lead with protection.",
             "PRD-INS-TERM", "single-income household"),
            ("Surplus balance deployment", "Open", "Medium",
             "Idle savings balance well above the transaction need; ladder into deposits.",
             "PRD-TD-LADDER", "avg balance trend"),
            ("Card usage is convenience-only", "Watch", "Low",
             "Low utilisation and full repayment; no credit conversation needed.", "", "utilisation ~18%"),
            ("Nominee and KYC hygiene", "Closed", "Low",
             "Nominee updated and KYC current; nothing outstanding.", "", "documents complete"),
            ("Relationship deepening", "Open", "Medium",
             "Long tenure, clean conduct - the value conversation, not a sales pitch.", "", "tenure + conduct"),
        ],
        "recovering": [
            ("Arrear cured - confirm in writing", "Action needed", "High",
             "Historic arrear is cured; issue the confirmation before anything else.",
             "", "1 cured EMI bounce"),
            ("Rate review after 12 clean months", "Open", "High",
             "Repayment record now supports a rate-review submission.", "PRD-PL-RATE", "12 on-time EMIs"),
            ("Card-to-loan consolidation", "Open", "Medium",
             "Moving the card balance into the loan cuts the blended cost.",
             "PRD-CC-EMI", "card at 42% APR"),
            ("Card limit restoration", "Watch", "Medium",
             "Limit was cut during the stress period; restoration needs a fresh assessment.",
             "", "limit reduced in FY24-25"),
            ("Confidence rebuild", "Open", "Medium",
             "Customer expects to be judged on the bad year; acknowledge the recovery explicitly.",
             "", "sentiment on last 3 calls"),
        ],
    }[persona]
    for i, (topic, status, prio, angle, products, evidence) in enumerate(thread_defs, start=1):
        t["engagement_threads"].append({
            "customer_id": cid, "thread_id": f"TH-{tag}-{i}", "topic": topic, "status": status,
            "priority": prio, "stakeholder_id": f"STK-{tag}-1", "angle": angle, "products": products,
            "evidence": evidence, "channel": "", "opened_date": "", "last_activity_date": "", "summary": "",
        })

    opp_defs = {
        "stress": [
            ("Credit Limit Increase", "Not eligible", "Blocked",
             "Declining income; 2 EMI bounces (SMA-1); high utilisation; open dispute; re-KYC due"),
            ("Card EMI Conversion / Balance Transfer", "Qualified", "Open", "Cuts 42% APR; service recovery"),
            ("Personal Loan Restructuring", "Qualified", "Open", "SMA-1; restructuring over new credit"),
            ("New Top-up Loan", "Hold", "Blocked", "Resolve dispute + collections first"),
        ],
        "stable": [
            ("Term Protection Cover", "Qualified", "Open", "Single-income household with no term cover"),
            ("Deposit Ladder", "Qualified", "Open", "Surplus balance idle in savings"),
            ("Credit Limit Increase", "Qualified", "Open", "Low utilisation, clean conduct, income growing"),
            ("Personal Loan Top-up", "Not eligible", "Blocked", "No borrowing need evidenced"),
        ],
        "recovering": [
            ("Interest Rate Review", "Qualified", "Open", "12 consecutive on-time EMIs after the cured arrear"),
            ("Card EMI Conversion / Balance Transfer", "Qualified", "Open", "Cuts 42% APR on the residual balance"),
            ("Credit Limit Restoration", "Hold", "Blocked", "Needs a fresh assessment; arrear cured only recently"),
            ("New Top-up Loan", "Not eligible", "Blocked", "Too soon after the arrear; consolidate first"),
        ],
    }[persona]
    for i, (otype, stage, status, blockers) in enumerate(opp_defs, start=1):
        t["opportunities"].append({
            "opportunity_id": f"OPP-{tag}-{i:03d}", "customer_id": cid, "opportunity_type": otype,
            "stage": stage, "recommended_band_inr": "", "status": status, "blockers": blockers,
        })

    int_defs = {
        "stress": [
            ("2026-02-20", "Service ticket", "Card transaction dispute",
             "Customer reported an unauthorised card transaction of Rs 48500 at GlobalMart Online; "
             "chargeback raised and card hot-listed.",
             "Confirm last genuine txn", "Process chargeback within SLA", "Negative"),
            ("2026-02-22", "Call", "EMI bounce",
             "Two personal-loan EMIs bounced for insufficient balance; customer cited a delayed client payment.",
             "Fund the account by due date", "Offer restructuring options", "Concerned"),
            ("2026-03-01", "Call", "Collections - SMA-1",
             "Account flagged SMA-1; RM discussed a one-time EMI deferral and a step-down plan.",
             "Decide on restructuring", "Share moratorium / step-down terms", "Concerned"),
            ("2026-03-10", "Branch meeting", "Card utilisation",
             "Card running at ~92% utilisation revolving at 42% APR; RM advised a balance-transfer / "
             "EMI conversion to cut interest.",
             "Choose EMI conversion", "Compute EMI-conversion savings", "Neutral"),
            ("2026-03-20", "Call", "Late-fee dispute",
             "Customer disputed a late-payment fee; RM logged it for review.",
             "Await fee review outcome", "Review late fee waiver eligibility", "Negative"),
            ("2026-04-08", "Call", "Retention risk",
             "Customer mentioned closing the card and moving to another bank over the dispute and charges.",
             "Hold closure decision", "Escalate to Case-Dispute RM + Branch Manager", "Negative"),
        ],
        "stable": [
            ("2026-02-11", "Call", "Standing instruction setup",
             "Customer asked to automate the monthly card payment from the salary account.",
             "Confirm the debit date", "Set up the standing instruction", "Positive"),
            ("2026-02-18", "Branch meeting", "Nominee update",
             "Nominee details refreshed at the branch; documents complete.",
             "Nothing outstanding", "Confirm the CBS update", "Neutral"),
            ("2026-02-26", "Call", "Protection gap",
             "Single-income household with no term cover; RM raised the protection conversation.",
             "Discuss with spouse", "Share indicative term cover", "Neutral"),
            ("2026-03-02", "Call", "Deposit rate query",
             "Customer asked what rate a 400-day deposit earns against the idle savings balance.",
             "Decide the deposit amount", "Share the rate card", "Positive"),
            ("2026-03-16", "Call", "Limit headroom",
             "Utilisation running near 18%; genuine headroom exists but no borrowing need was expressed.",
             "None", "Note the headroom, do not push", "Neutral"),
            ("2026-04-06", "Call", "Relationship review",
             "Annual relationship review; conduct clean across every product.",
             "Continue as is", "Schedule the next review", "Positive"),
        ],
        "recovering": [
            ("2026-01-22", "Service ticket", "Arrear clearance",
             "Customer asked for written confirmation that the historic EMI arrear is fully cured.",
             "Await the letter", "Issue the confirmation letter", "Concerned"),
            ("2026-02-14", "Call", "Rate review request",
             "Twelve consecutive on-time EMIs; customer asked for the rate to be revisited.",
             "Keep repayments regular", "Prepare the rate-review submission", "Neutral"),
            ("2026-02-27", "Call", "Card limit restoration",
             "Customer asked when the limit reduced during the stress period can be restored.",
             "Await the assessment", "Explain the restoration criteria", "Concerned"),
            ("2026-03-05", "Branch meeting", "Consolidation options",
             "RM modelled moving the residual card balance into the personal loan to cut the blended rate.",
             "Review the numbers", "Share the consolidation illustration", "Positive"),
            ("2026-03-19", "Call", "Bureau score progress",
             "Bureau score has recovered into the near-prime band; RM walked through what still drags it.",
             "Avoid new enquiries", "Re-check the score in 90 days", "Positive"),
            ("2026-04-07", "Call", "Confidence check-in",
             "Customer wanted reassurance that the bad year no longer defines the relationship.",
             "Stay in touch", "Acknowledge the recovery on every call", "Neutral"),
        ],
    }[persona]
    for i, (idate, channel, subject, summary, cc, cb, sentiment) in enumerate(int_defs, start=1):
        t["interactions"].append({
            "interaction_id": f"INT-{tag}-{i:03d}", "customer_id": cid, "rm_id": RM_ID,
            "interaction_date": idate, "channel": channel, "subject": subject, "summary": summary,
            "commitments_by_customer": cc, "commitments_by_bank": cb,
            "next_follow_up_date": "2026-04-18", "sentiment": sentiment,
            "linked_task_id": f"TASK-{tag}-{min(i, len(task_defs)):03d}", "created_by": "RM",
        })
    _ = name


def generate_rm_activity(seed: str, n_customers: int) -> list[dict]:
    r = rng_for(seed, "rm", RM_ID)
    rows = []
    start = date(2026, 4, 15)
    for i in range(30):
        d = start + timedelta(days=i)
        weekend = d.weekday() >= 5
        sla_due = r.randint(6, 12)
        rows.append({
            "rm_id": RM_ID, "activity_date": d.isoformat(),
            "calls_made": r.randint(2, 5) if weekend else r.randint(6, 12),
            "meetings_held": r.randint(0, 1) if weekend else r.randint(1, 5),
            "tasks_closed": r.randint(1, 4) if weekend else r.randint(4, 9),
            "documents_collected": r.randint(0, 3) if weekend else r.randint(2, 10),
            "opportunities_logged": r.randint(0, 2) if weekend else r.randint(1, 5),
            "tickets_resolved": r.randint(0, 2) if weekend else r.randint(1, 5),
            "sla_due": sla_due, "sla_met": max(0, sla_due - r.randint(0, 2)),
            "portfolio_credits_inr": r.randint(18_000_000, 32_000_000) * n_customers,
        })
    return rows


# ---------------------------------------------------------------------------
# AI-enriched CRM case narratives (de-templated)
# ---------------------------------------------------------------------------
# The previous pack ran every case through ONE sentence frame. Each frame below
# is picked from the case's own facts (type, sentiment, status, whether the case
# is open, whether documents are blocking), so neighbouring cases read
# differently instead of repeating a template.
_OPENERS = [
    "{who} is sitting with an unresolved {topic}, logged as {status}.",
    "The live thread here is {topic}; the record still shows it as {status}.",
    "{topic} is the reason this case is on the RM's desk today - current state: {status}.",
    "What {who} actually raised was {topic}, and nothing has moved it off {status}.",
    "This one is straightforward on paper: {topic}, still {status}.",
    "{topic} has been carried across more than one contact and remains {status}.",
]
_CLOSED_OPENERS = [
    "{topic} is closed, and it is useful history rather than an open task.",
    "This {topic} case was settled already; it matters only as context now.",
    "Nothing to action on {topic} - it closed cleanly.",
]
_BRIDGES = [
    "The note on file reads: {note}",
    "Verbatim from the CRM: {note}",
    "The RM's own summary was: {note}",
    "What was recorded at the time: {note}",
]
_CONTEXTS = [
    "Read against {ctx}, that changes the priority.",
    "Set that beside {ctx} and the shape of the conversation is obvious.",
    "It has to be read with {ctx} in view.",
    "None of it stands alone - {ctx} is the backdrop.",
]
_POSITIONS = {
    "Negative": [
        "Expect frustration. {first} has been let down once already and will be listening for whether the bank owns it.",
        "{first} is angry rather than confused - the facts are not in dispute, the resolution is.",
        "This is a trust problem now, not an information problem. {first} wants to hear a commitment, not a process.",
    ],
    "Concerned": [
        "{first} is worried about what happens next and is looking for a realistic plan, not reassurance.",
        "The anxiety here is about affordability. {first} needs options laid out plainly.",
        "{first} is cooperative but stretched - avoid anything that sounds like a new obligation.",
    ],
    "Neutral": [
        "{first} is neither pushing nor resisting; the conversation is the RM's to shape.",
        "No strong feeling either way from {first} - this is an information exchange.",
        "{first} is simply waiting on a fact. Give it, then move on.",
    ],
    "Positive": [
        "{first} is receptive here, which is the moment to be useful rather than salesy.",
        "Goodwill exists on this one - {first} came to the bank rather than the other way round.",
        "{first} is engaged. The risk is over-selling into an open door.",
    ],
}
_NEXT_STEPS = [
    "Open with this case, confirm one fact you do not already have, and log the answer before offering anything.",
    "Use it as continuity - reference it by name so {first} knows the bank remembered, then ask the confirming question.",
    "Confirm the current position out loud, capture any new amount or date as a CRM fact, and only then discuss options.",
    "Do not re-litigate the history. Confirm where it stands, then move to the single decision that is actually open.",
    "Ask one question, write down the answer, and raise a task only if {first} explicitly asks for it.",
]


def _discussion_points(case: dict, docs_note: str, rnd: random.Random) -> list[str]:
    topic = case["title"]
    status = case["status"]
    pool = [
        f"Lead with {topic.lower()} - do not let it surface later as a surprise",
        f"Confirm the customer still recognises the position recorded as '{status}'",
        f"Check what changed since the note was written, and by how much",
        f"Document context: {docs_note}" if docs_note else "Confirm no document is blocking the next step",
        "Capture any new amount, counterparty, date or commitment as a CRM fact",
        "Agree explicitly who does the next thing, and by when",
        "Ask whether anything else has been raised through another channel",
    ]
    rnd.shuffle(pool)
    return pool[:4]


def build_enriched_cases(tables: dict, seed: str) -> list[dict]:
    """One enriched record per deterministic case id across interactions,
    opportunities, tasks and service requests."""
    out = []
    by_customer_name = {r["customer_id"]: r["display_name"] for r in tables.get("customer_master", [])}
    by_customer_ctx = {r["customer_id"]: r["business_model_notes"] for r in tables.get("business_profile", [])}
    docs_by_customer: dict[str, list[str]] = {}
    for d in tables.get("documents", []):
        if d["status"] == "Pending":
            docs_by_customer.setdefault(d["customer_id"], []).append(f"{d['document_type']} (Pending)")

    def case(cid, case_id, case_type, title, status, note):
        return {"customer_id": cid, "case_id": case_id, "case_type": case_type,
                "title": title, "status": status, "note": note}

    cases = []
    for r in tables.get("interactions", []):
        cases.append(case(r["customer_id"], r["interaction_id"], "RM interaction",
                          r["subject"], r["sentiment"], r["summary"]))
    for r in tables.get("opportunities", []):
        cases.append(case(r["customer_id"], r["opportunity_id"], "Opportunity",
                          r["opportunity_type"], r["stage"],
                          f"Stage {r['stage']}; status {r['status']}. {r['blockers']}"))
    for r in tables.get("crm_tasks", []):
        cases.append(case(r["customer_id"], r["task_id"], "CRM task",
                          r["title"], r["status"], f"Due {r['due_date']}, priority {r['priority']}."))
    for r in tables.get("service_requests", []):
        cases.append(case(r["customer_id"], r["ticket_id"], "Service request",
                          r["category"], r["status"], r["description"]))

    for idx, c in enumerate(cases):
        rnd = rng_for(seed, c["case_id"], "narrative")
        who = by_customer_name.get(c["customer_id"], c["customer_id"])
        first = who.split()[0]
        ctx = (by_customer_ctx.get(c["customer_id"]) or "the wider relationship").rstrip(".")
        docs_note = ", ".join(docs_by_customer.get(c["customer_id"], [])) or ""
        closed = c["status"] in ("Closed", "Blocked", "Not eligible")
        note = str(c["note"]).rstrip(".") + "."

        opener = rnd.choice(_CLOSED_OPENERS if closed else _OPENERS).format(
            who=who, topic=c["title"], status=c["status"])
        bridge = rnd.choice(_BRIDGES).format(note=note)
        context = rnd.choice(_CONTEXTS).format(ctx=ctx)
        # Vary structure, not just wording: some cases lead with the note, some
        # with the context, some drop the context sentence entirely.
        shape = idx % 3
        if shape == 0:
            narrative = f"{opener} {bridge} {context}"
        elif shape == 1:
            narrative = f"{bridge} {opener} {context}"
        else:
            narrative = f"{opener} {bridge}"
        narrative = narrative[:1].upper() + narrative[1:]

        sentiment_key = c["status"] if c["status"] in _POSITIONS else (
            "Negative" if closed is False and c["status"] in ("Open", "Action needed") else "Neutral")
        position = rnd.choice(_POSITIONS[sentiment_key]).format(first=first)

        rich = {
            "narrative": narrative,
            "discussion_points": _discussion_points(c, docs_note, rnd),
            "current_status_detail": (
                f"Recorded as {c['status']}."
                + (f" Outstanding documents: {docs_note}." if docs_note else " No document is blocking this.")
                + (" Closed cases are context only." if closed else " This is still open.")),
            "customer_position": position,
            "rm_next_step": rnd.choice(_NEXT_STEPS).format(first=first),
        }
        out.append({"case_id": c["case_id"], "customer_id": c["customer_id"], "case_type": c["case_type"],
                    "title": c["title"], "status": c["status"],
                    "rich_json": json.dumps(rich, ensure_ascii=False)})
    return out


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
FILE_MAP = [
    ("customer_master", "01_master_data/customer_master.csv"),
    ("business_profile", "01_master_data/msme_business_profile.csv"),
    ("portfolio_assignments", "01_master_data/portfolio_assignments.csv"),
    ("promoters", "01_master_data/promoters_guarantors.csv"),
    ("stakeholders", "01_master_data/stakeholders.csv"),
    ("accounts", "02_accounts/accounts.csv"),
    ("counterparties", "02_accounts/counterparty_master.csv"),
    ("transactions", "02_accounts/current_account_transactions_fy2025_26.csv"),
    ("daily_balances", "02_accounts/daily_balances.csv"),
    ("loan_facilities", "03_credit/loan_facilities.csv"),
    ("daily_limit_utilization", "03_credit/daily_limit_utilization.csv"),
    ("repayments", "03_credit/repayment_history.csv"),
    ("bureau", "04_financials/bureau_summary.csv"),
    ("financials", "04_financials/financial_statements_summary.csv"),
    ("cheque_returns", "05_operations/cheque_returns.csv"),
    ("consent", "05_operations/consent_registry.csv"),
    ("documents", "05_operations/document_status.csv"),
    ("service_requests", "05_operations/service_requests.csv"),
    ("audit_log", "06_crm/audit_log.csv"),
    ("crm_tasks", "06_crm/crm_tasks.csv"),
    ("engagement_threads", "06_crm/engagement_threads.csv"),
    ("opportunities", "06_crm/opportunities.csv"),
    ("interactions", "06_crm/rm_interactions.csv"),
    ("rm_activity", "08_rm/rm_daily_activity.csv"),
]

# Header-only files the retail pack intentionally leaves empty (MSME-only schemas
# the backend still probes). Emitted so the shape is complete and stable.
HEADER_ONLY = {
    "02_accounts/transaction_category_rules.csv":
        (["keyword", "category_lvl1", "category_lvl2"],
         [["Salary", "Income", "Salary credit"], ["EMI", "EMI", "Loan EMI"],
          ["Card", "Card spend", "Retail purchase"], ["ATM", "Cash", "ATM withdrawal"],
          ["SIP", "Investment", "SIP debit"]]),
    "03_credit/collateral_security.csv":
        (["collateral_id", "customer_id", "facility_id", "collateral_type", "description", "owner_name",
          "valuation_inr", "valuation_date", "margin_pct", "insurance_required_flag",
          "insurance_valid_until", "charge_status", "remarks"], []),
    "03_credit/facility_covenants.csv":
        (["covenant_id", "facility_id", "customer_id", "covenant_type", "requirement_text", "frequency",
          "due_date", "status", "last_received_date", "severity", "breach_action"], []),
    "03_credit/insurance_status.csv":
        (["insurance_id", "customer_id", "facility_id", "policy_type", "insurer", "coverage_scope",
          "valid_from", "valid_until", "status", "sum_insured_inr", "annual_premium_inr"], []),
    "03_credit/stock_statements.csv":
        (["statement_id", "customer_id", "facility_id", "period", "stock_value_inr", "receivables_inr",
          "drawing_power_inr", "status", "received_date"], []),
    "04_financials/debtor_creditor_aging.csv":
        (["customer_id", "as_of", "debtors_0_30_inr", "debtors_31_60_inr", "debtors_61_90_inr",
          "debtors_90_plus_inr", "creditors_total_inr", "remarks"], []),
    "04_financials/gst_returns_monthly.csv":
        (["period", "customer_id", "gst_sales_inr", "gst_purchases_inr", "tax_paid_inr", "filing_status",
          "filing_date", "variance_vs_bank_credits_pct", "trend_tag", "remarks"], []),
}


def write_csv(path: str, rows: list[dict], header: list[str] | None = None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cols = header or (list(rows[0].keys()) if rows else [])
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_pack(tables: dict, out_dir: str) -> dict:
    counts = {}
    for key, rel in FILE_MAP:
        rows = tables.get(key, [])
        write_csv(os.path.join(out_dir, rel), rows)
        counts[rel] = len(rows)
    for rel, (header, rows) in HEADER_ONLY.items():
        path = os.path.join(out_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(header)
            w.writerows(rows)
        counts[rel] = len(rows)
    return counts


def read_pack(csv_dir: str) -> dict:
    tables = {}
    for key, rel in FILE_MAP:
        path = os.path.join(csv_dir, rel)
        if os.path.exists(path):
            with open(path, newline="", encoding="utf-8") as f:
                tables[key] = list(csv.DictReader(f))
        else:
            tables[key] = []
    return tables


def _read_header(path: str) -> list[str] | None:
    if not os.path.exists(path):
        return None
    with open(path, newline="", encoding="utf-8") as f:
        try:
            return next(csv.reader(f))
        except StopIteration:
            return None


def append_csv(path: str, rows: list[dict]) -> int:
    """Append rows to an existing CSV WITHOUT rewriting a single existing byte.

    The file is opened in append mode and the existing header is reused as the
    field order, so committed rows keep their exact bytes, quoting and ordering.
    `extrasaction='raise'` turns a schema drift into a loud failure instead of a
    silently dropped column.
    """
    if not rows:
        return 0
    header = _read_header(path)
    if header is None:
        raise SystemExit(f"Cannot append: {path} is missing or has no header.")
    # A missing trailing newline would glue the first appended row onto the last
    # existing one, so top it up first (all pack files are LF per .gitattributes).
    with open(path, "rb") as f:
        if f.seek(0, os.SEEK_END) and f.tell() > 0:
            f.seek(-1, os.SEEK_END)
            needs_nl = f.read(1) != b"\n"
        else:
            needs_nl = False
    if needs_nl:
        with open(path, "a", newline="", encoding="utf-8") as f:
            f.write("\n")
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header, lineterminator="\n",
                           restval="", extrasaction="raise")
        for r in rows:
            w.writerow(r)
    return len(rows)


def run_validator(csv_dir: str) -> tuple[str, list[str], list[str]]:
    """Run validate_seed.py in-process against `csv_dir` so the manifest records
    a real result rather than an assumed one."""
    import importlib.util
    prev = os.environ.get("RTL_DATA_DIR")
    os.environ["RTL_DATA_DIR"] = csv_dir
    try:
        spec = importlib.util.spec_from_file_location(
            "_validate_seed_run", os.path.join(HERE, "validate_seed.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mod.main()
        return ("passed" if rc == 0 else "failed"), list(mod.ERRORS), list(mod.WARNINGS)
    except Exception as e:                                   # never block generation on this
        return "unknown", [f"validator could not run: {e}"], []
    finally:
        if prev is None:
            os.environ.pop("RTL_DATA_DIR", None)
        else:
            os.environ["RTL_DATA_DIR"] = prev


def write_manifest(kb_dir: str, csv_dir: str, seed: str, customers: int, counts: dict,
                   enriched: list[dict], case_ids: int):
    status, errors, warnings = run_validator(csv_dir)
    manifest = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z",
        "generator": "infra/phase3-data/generate_seed.py",
        "deterministic_seed": seed,
        "customers": customers,
        "deterministic_case_ids": case_ids,
        "cases_enriched": len(enriched),
        "validation_status": status,
        "validation_errors": errors,
        "validation_warnings": warnings,
        "row_counts": dict(sorted(counts.items())),
        "source_files": {
            "csv_dir": os.path.relpath(csv_dir, REPO_ROOT).replace(os.sep, "/"),
            "enriched_cases": os.path.relpath(
                os.path.join(kb_dir, "crm_cases_enriched.csv"), REPO_ROOT).replace(os.sep, "/"),
        },
    }
    os.makedirs(kb_dir, exist_ok=True)
    with open(os.path.join(kb_dir, "ai_generation_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return manifest


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
# Tables that are scoped to the RM rather than to a customer. Appending a
# customer must never duplicate these.
NON_CUSTOMER_TABLES = {"rm_activity"}

# Every id column the pack uses, so an append can prove up front that a new
# customer cannot collide with one already on disk.
ID_COLUMNS = {
    "customer_master": ["customer_id"], "promoters": ["person_id"],
    "stakeholders": ["stakeholder_id"], "accounts": ["account_id"],
    "counterparties": ["counterparty_id"], "transactions": ["txn_id"],
    "loan_facilities": ["facility_id"], "repayments": ["repayment_id"],
    "cheque_returns": ["return_id"], "documents": ["document_id"],
    "service_requests": ["ticket_id"], "audit_log": ["audit_id"],
    "crm_tasks": ["task_id"], "engagement_threads": ["thread_id"],
    "opportunities": ["opportunity_id"], "interactions": ["interaction_id"],
}


def assert_no_id_collisions(existing: dict, incoming: dict) -> None:
    clashes = []
    for key, cols in ID_COLUMNS.items():
        have = {r.get(c) for r in existing.get(key, []) for c in cols if r.get(c)}
        for r in incoming.get(key, []):
            for c in cols:
                if r.get(c) and r[c] in have:
                    clashes.append(f"{key}.{c}={r[c]}")
    if clashes:
        raise SystemExit("Refusing to append — id collision with the existing pack:\n  "
                         + "\n  ".join(sorted(set(clashes))[:20]))


def _partially_appended(existing: dict, customer_ids: set[str]) -> dict[str, list[str]]:
    """Report customers that appear in some per-customer tables but not all.

    Every persona emits at least one row in each of these tables, so an absence
    means a previous append died midway rather than being a legitimate shape.
    """
    REQUIRED = ["customer_master", "business_profile", "portfolio_assignments", "promoters",
                "stakeholders", "accounts", "counterparties", "transactions", "daily_balances",
                "loan_facilities", "daily_limit_utilization", "repayments", "bureau",
                "financials", "consent", "documents", "service_requests", "audit_log",
                "crm_tasks", "engagement_threads", "opportunities", "interactions"]
    rel_by_key = dict(FILE_MAP)
    out: dict[str, list[str]] = {}
    for cid in sorted(customer_ids):
        missing = [rel_by_key.get(k, k) for k in REQUIRED
                   if not any(r.get("customer_id") == cid for r in existing.get(k, []))]
        if missing and len(missing) != len(REQUIRED):
            out[cid] = missing
    return out


def append_customers(args) -> int:
    """APPEND-ONLY mode.

    Treats every customer already in the pack as an immutable fixture: their
    rows are never re-derived, re-serialised or reordered. Only customers that
    are absent are generated, and their rows are appended to the end of each
    per-customer CSV.

    This is what lets the demo carry a multi-customer portfolio while Rakesh
    (CTB-RTL-002) stays byte-identical to what is committed.
    """
    existing = read_pack(args.out)
    if not existing.get("customer_master"):
        raise SystemExit(f"No pack found at {args.out} — run a full generation first.")
    existing_ids = {r["customer_id"] for r in existing["customer_master"]}

    n = max(1, min(args.customers, 1 + len(EXTRA_CUSTOMERS)))
    specs = [RAKESH] + EXTRA_CUSTOMERS[: n - 1]

    incoming: dict[str, list[dict]] = {}
    added = []
    for i, spec in enumerate(specs):
        if spec["customer_id"] in existing_ids:
            continue                              # never re-derive an existing customer
        # `i` is the customer's canonical index, so the generated ids and random
        # streams are identical to what a full --customers N run would produce.
        for k, rows in generate_customer(spec, args.seed, i).items():
            incoming.setdefault(k, []).extend(rows)
        added.append(spec["customer_id"])

    if not added:
        # A customer is only "already appended" if it is present in EVERY
        # per-customer table, not just customer_master (which is written first).
        # Otherwise a half-finished append would silently report success.
        partial = _partially_appended(existing, {s["customer_id"] for s in specs} & existing_ids)
        if partial:
            raise SystemExit(
                "Refusing to continue — these customers are only PARTIALLY present, which means an "
                "earlier append did not finish:\n  "
                + "\n  ".join(f"{cid}: missing from {', '.join(tables)}" for cid, tables in partial.items())
                + "\n\nRestore the pack (git checkout -- data/csv) and re-run.")
        print(f"[=] Nothing to append — {sorted(existing_ids)} already present.")
        return 0

    assert_no_id_collisions(existing, incoming)

    # PRE-FLIGHT every row against the on-disk header BEFORE opening a single
    # file for append. The write loop is not atomic and writes straight into the
    # committed pack, so a failure partway through would leave the new customer
    # present in customer_master (written first) but missing from every table
    # after the failure point — and the skip-if-present check below would then
    # report "nothing to append" on the retry, hiding the damage. Validating up
    # front turns schema drift into a clean refusal that touches nothing.
    problems = []
    for key, rel in FILE_MAP:
        if key in NON_CUSTOMER_TABLES:
            continue
        rows = incoming.get(key) or []
        if not rows:
            continue
        path = os.path.join(args.out, rel)
        header = _read_header(path)
        if header is None:
            problems.append(f"{rel}: missing or has no header")
            continue
        allowed = set(header)
        for r in rows:
            unknown = [k for k in r if k not in allowed]
            if unknown:
                problems.append(f"{rel}: generated columns not in the on-disk header: {sorted(unknown)}")
                break
    if problems:
        raise SystemExit("Refusing to append — the pack on disk does not match what the "
                         "generator produces:\n  " + "\n  ".join(problems))

    print(f"[+] Appending {len(added)} customer(s): {', '.join(added)}")
    for key, rel in FILE_MAP:
        if key in NON_CUSTOMER_TABLES:
            continue
        rows = incoming.get(key) or []
        if not rows:
            continue
        wrote = append_csv(os.path.join(args.out, rel), rows)
        print(f"      +{wrote:<5} {rel}")

    # Enriched narratives for the new customers only; the existing rows are
    # left exactly as they are.
    new_cases = build_enriched_cases(incoming, args.seed)
    kb_path = os.path.join(args.kb, "crm_cases_enriched.csv")
    wrote = append_csv(kb_path, new_cases)
    print(f"      +{wrote:<5} {os.path.relpath(kb_path, REPO_ROOT).replace(os.sep, '/')}")

    # Manifest reflects the WHOLE pack after the append, validated in-process.
    merged = read_pack(args.out)
    counts = {rel: len(merged.get(key, [])) for key, rel in FILE_MAP}
    with open(kb_path, newline="", encoding="utf-8") as f:
        all_cases = list(csv.DictReader(f))
    m = write_manifest(args.kb, args.out, args.seed,
                       len(merged["customer_master"]), counts, all_cases, len(all_cases))
    print(f"[+] Pack now holds {len(merged['customer_master'])} customer(s), "
          f"{len(all_cases)} enriched case(s)")
    print(f"[+] Validation: {m['validation_status']}"
          + (f" ({len(m['validation_errors'])} error(s))" if m["validation_errors"] else ""))
    for e in m["validation_errors"]:
        print(f"      [FAIL] {e}")
    return 0 if m["validation_status"] == "passed" else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Deterministic retail seed generator (offline, stdlib only).")
    ap.add_argument("--out", default=os.path.join(REPO_ROOT, "data", "csv"),
                    help="CSV output directory (default: data/csv)")
    ap.add_argument("--kb", default=os.path.join(REPO_ROOT, "data", "knowledge_base"),
                    help="knowledge-base output directory (default: data/knowledge_base)")
    ap.add_argument("--customers", type=int, default=1,
                    help=f"number of customers, 1..{1 + len(EXTRA_CUSTOMERS)} (1 = Rakesh only)")
    ap.add_argument("--seed", default=DEFAULT_SEED, help="deterministic seed string")
    ap.add_argument("--enrich-only", action="store_true",
                    help="do not touch the CSVs; rebuild only the enriched cases + manifest "
                         "from the pack already on disk")
    ap.add_argument("--append", action="store_true",
                    help="APPEND-ONLY: keep every customer already in the pack byte-identical "
                         "and only append the customers that are missing (up to --customers)")
    args = ap.parse_args(argv)

    if args.append and args.enrich_only:
        print("[!] --append and --enrich-only are mutually exclusive.", file=sys.stderr)
        return 2
    if args.append:
        return append_customers(args)

    if args.enrich_only:
        tables = read_pack(args.out)
        if not tables.get("customer_master"):
            print(f"[!] No pack found at {args.out}", file=sys.stderr)
            return 1
        enriched = build_enriched_cases(tables, args.seed)
        write_csv(os.path.join(args.kb, "crm_cases_enriched.csv"), enriched,
                  header=["case_id", "customer_id", "case_type", "title", "status", "rich_json"])
        counts = {rel: len(tables.get(key, [])) for key, rel in FILE_MAP}
        m = write_manifest(args.kb, args.out, args.seed,
                           len(tables["customer_master"]), counts, enriched, len(enriched))
        print(f"[+] Enriched {len(enriched)} case(s) from the existing pack at {args.out}")
        print(f"[+] Manifest validation_status = {m['validation_status']}")
        return 0

    n = max(1, min(args.customers, 1 + len(EXTRA_CUSTOMERS)))
    if n != args.customers:
        print(f"[!] --customers clamped to {n} (max {1 + len(EXTRA_CUSTOMERS)})", file=sys.stderr)

    specs = [RAKESH] + EXTRA_CUSTOMERS[: n - 1]
    merged: dict[str, list[dict]] = {}
    for i, spec in enumerate(specs):
        tabs = generate_customer(spec, args.seed, i)
        for k, rows in tabs.items():
            merged.setdefault(k, []).extend(rows)
    merged["rm_activity"] = generate_rm_activity(args.seed, n)

    counts = write_pack(merged, args.out)
    enriched = build_enriched_cases(merged, args.seed)
    write_csv(os.path.join(args.kb, "crm_cases_enriched.csv"), enriched,
              header=["case_id", "customer_id", "case_type", "title", "status", "rich_json"])
    m = write_manifest(args.kb, args.out, args.seed, n, counts, enriched, len(enriched))

    print(f"[+] Generated {n} customer(s) with seed '{args.seed}' into {args.out}")
    for rel in sorted(counts):
        print(f"      {counts[rel]:>5}  {rel}")
    print(f"[+] Enriched cases: {len(enriched)}")
    print(f"[+] Validation: {m['validation_status']}"
          + (f" ({len(m['validation_errors'])} error(s))" if m["validation_errors"] else ""))
    return 0 if m["validation_status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
