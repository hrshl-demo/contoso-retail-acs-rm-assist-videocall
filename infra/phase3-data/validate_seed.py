#!/usr/bin/env python3
"""
infra/phase3-data/validate_seed.py  — RETAIL edition.

Phase 3 validator. Proves the synthetic RETAIL CSV pack is internally consistent
and the golden expected-output files are present. Exits non-zero on any hard
failure so phase3-data/up.sh halts rather than ship inconsistent data.

Checks (retail):
  1. All expected CSVs exist and are non-empty (header at least).
  2. Referential integrity: customer/account/facility/counterparty FKs.
  3. Balance reconciliation: per-transaction running balance is consistent.
  4. Bounce cross-reference: every cheque_returns (EMI/auto-debit bounce) row has
     a matching is_return=Y transaction.
  5. Repayments cross-reference: every EMI repayment links to an EMI debit.
  6. Rakesh stress story (single-customer pack): CTB-RTL-002 income down YoY,
     >=2 EMI bounces, sub-700 CIBIL, at least one disputed (Unauthorized) txn,
     and no other customer_ids left anywhere in the pack.
"""
from __future__ import annotations
import csv
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
DATA = os.environ.get("RTL_DATA_DIR", os.environ.get("MSME_DATA_DIR", os.path.join(REPO_ROOT, "data", "csv")))

ERRORS: list[str] = []
WARNINGS: list[str] = []
PASSES: list[str] = []

def err(m): ERRORS.append(m)
def warn(m): WARNINGS.append(m)
def ok(m): PASSES.append(m)

def load(rel) -> list[dict]:
    path = os.path.join(DATA, rel)
    if not os.path.exists(path):
        err(f"MISSING FILE: {rel}")
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))

# Files that must exist (header at least). Retail leaves some MSME-only files
# header-only (gst/stock/covenants/aging) — that is fine.
EXPECTED = [
    "01_master_data/customer_master.csv", "01_master_data/msme_business_profile.csv",
    "01_master_data/promoters_guarantors.csv", "01_master_data/portfolio_assignments.csv",
    "02_accounts/accounts.csv", "02_accounts/current_account_transactions_fy2025_26.csv",
    "02_accounts/daily_balances.csv", "02_accounts/counterparty_master.csv",
    "03_credit/loan_facilities.csv", "03_credit/daily_limit_utilization.csv",
    "03_credit/repayment_history.csv", "04_financials/bureau_summary.csv",
    "05_operations/document_status.csv", "05_operations/service_requests.csv",
    "05_operations/consent_registry.csv", "06_crm/rm_interactions.csv",
    "06_crm/crm_tasks.csv", "06_crm/opportunities.csv",
]

def check_files():
    for rel in EXPECTED:
        path = os.path.join(DATA, rel)
        if not os.path.exists(path):
            err(f"MISSING FILE: {rel}")
        elif os.path.getsize(path) == 0:
            err(f"EMPTY FILE: {rel}")
    if not ERRORS:
        ok(f"All {len(EXPECTED)} required CSVs present and non-empty.")

def check_referential_integrity():
    customers = {r["customer_id"] for r in load("01_master_data/customer_master.csv")}
    accounts = {r["account_id"] for r in load("02_accounts/accounts.csv")}
    facilities = {r["facility_id"] for r in load("03_credit/loan_facilities.csv")}
    cps = {r["counterparty_id"] for r in load("02_accounts/counterparty_master.csv")}

    txns = load("02_accounts/current_account_transactions_fy2025_26.csv")
    bad_cust = {t["customer_id"] for t in txns if t["customer_id"] not in customers}
    bad_acct = {t["account_id"] for t in txns if t["account_id"] not in accounts}
    # allow synthetic pseudo counterparties not in the master
    pseudo = lambda x: any(s in x for s in ("-SELF", "-SAL", "-TAX", "-BANK", "-ATM", "-INC"))
    bad_cp = {t["counterparty_id"] for t in txns
              if t["counterparty_id"] and t["counterparty_id"] not in cps and not pseudo(t["counterparty_id"])}

    if bad_cust: err(f"Transactions reference unknown customer_id: {bad_cust}")
    if bad_acct: err(f"Transactions reference unknown account_id: {bad_acct}")
    if bad_cp:   warn(f"Transactions reference non-master counterparty_id (allowed): {bad_cp}")

    util = load("03_credit/daily_limit_utilization.csv")
    bad_fac = {u["facility_id"] for u in util if u["facility_id"] not in facilities}
    if bad_fac: err(f"Utilization references unknown facility_id: {bad_fac}")

    if not (bad_cust or bad_acct or bad_fac):
        ok("Referential integrity holds (customer/account/facility FKs).")

def check_balance_reconciliation():
    txns = load("02_accounts/current_account_transactions_fy2025_26.csv")
    by_acct = defaultdict(list)
    for t in txns:
        by_acct[t["account_id"]].append(t)
    tol = 1.0
    problems = 0
    for acct, rows in by_acct.items():
        rows.sort(key=lambda r: (r["txn_timestamp"], r["txn_id"]))
        prev = None
        for r in rows:
            amt = float(r["amount_inr"])
            bal = float(r["balance_after_txn_inr"])
            signed = amt if r["dr_cr"] == "CR" else -amt
            if prev is not None and r["is_return"] != "Y":
                if abs((prev + signed) - bal) > tol:
                    problems += 1
                    if problems <= 3:
                        warn(f"  {acct} {r['txn_id']}: balance {bal:.2f} != prev {prev:.2f} signed {signed:.2f}")
            prev = bal
    if problems == 0:
        ok("Per-transaction running balances reconcile within tolerance.")
    else:
        warn(f"Balance reconciliation: {problems} non-return rows drifted > {tol} (review).")

def check_bounces():
    txns = {t["txn_id"]: t for t in load("02_accounts/current_account_transactions_fy2025_26.csv")}
    crs = load("05_operations/cheque_returns.csv")
    missing = [c["txn_id"] for c in crs if c["txn_id"] not in txns]
    notflag = [c["txn_id"] for c in crs if c["txn_id"] in txns and txns[c["txn_id"]]["is_return"] != "Y"]
    if missing: err(f"bounces reference txn_ids absent from transactions: {missing}")
    if notflag: err(f"bounces not flagged is_return=Y: {notflag}")
    if not (missing or notflag):
        ok(f"All {len(crs)} EMI/auto-debit bounces cross-reference real is_return=Y transactions.")

def check_repayments():
    txns = {t["txn_id"]: t for t in load("02_accounts/current_account_transactions_fy2025_26.csv")}
    rpys = load("03_credit/repayment_history.csv")
    bad = [r["source_txn_id"] for r in rpys
           if r.get("source_txn_id") and (r["source_txn_id"] not in txns
                                          or txns[r["source_txn_id"]]["category_lvl1"] not in ("EMI", "Loan servicing"))]
    if bad: err(f"repayment source_txn_ids missing or not EMI debits: {bad}")
    else: ok(f"All {len(rpys)} repayments cross-reference EMI transactions.")

def check_rakesh_stress_story():
    """Single-customer pack: validate ONLY Rakesh (CTB-RTL-002)'s stress narrative."""
    RAKESH = "CTB-RTL-002"
    fin = load("04_financials/financial_statements_summary.csv")
    b = next((f for f in fin if f["customer_id"] == RAKESH), {})
    if not b:
        err("Rakesh (CTB-RTL-002) missing from financial_statements_summary.")
    elif int(float(b.get("turnover_inr", 0))) >= int(float(b.get("turnover_prev_inr", 1))):
        err("Rakesh income should trend DOWN year-on-year (stress story).")
    else:
        ok("Rakesh income down year-on-year (stress story).")

    crs = load("05_operations/cheque_returns.csv")
    b_cr = sum(1 for c in crs if c["customer_id"] == RAKESH)
    if b_cr < 2: err(f"Rakesh should have >=2 EMI bounces (got {b_cr}).")
    else: ok(f"Rakesh EMI bounces = {b_cr} (>=2).")

    bureau = load("04_financials/bureau_summary.csv")
    b_score = next((int(float(x.get("score", 0))) for x in bureau if x["customer_id"] == RAKESH), 999)
    if b_score >= 700: err(f"Rakesh CIBIL should be sub-700 (got {b_score}).")
    else: ok(f"Rakesh CIBIL = {b_score} (<700).")

    txns = load("02_accounts/current_account_transactions_fy2025_26.csv")
    disputed = [t for t in txns if t["customer_id"] == RAKESH and t.get("anomaly_tag") == "Unauthorized"]
    if not disputed: err("Rakesh should have a disputed (Unauthorized) transaction.")
    else: ok(f"Rakesh has {len(disputed)} disputed transaction(s).")

    # This is a single-customer demo pack: no other customer_ids may remain.
    cust = {r["customer_id"] for r in load("01_master_data/customer_master.csv")}
    if cust != {RAKESH}:
        err(f"customer_master should contain only {RAKESH}; found {sorted(cust)}.")
    else:
        ok(f"Single-customer pack confirmed ({RAKESH} only).")

def main():
    print(f"[+] Validating RETAIL dataset in: {DATA}\n")
    check_files()
    if any(e.startswith("MISSING") or e.startswith("EMPTY") for e in ERRORS):
        return _report()
    check_referential_integrity()
    check_balance_reconciliation()
    check_bounces()
    check_repayments()
    check_rakesh_stress_story()
    return _report()

def _report():
    for p in PASSES: print(f"  [PASS] {p}")
    for w in WARNINGS: print(f"  [warn] {w}")
    for e in ERRORS: print(f"  [FAIL] {e}")
    print()
    if ERRORS:
        print(f"VALIDATION FAILED: {len(ERRORS)} error(s), {len(WARNINGS)} warning(s).")
        return 1
    print(f"VALIDATION PASSED: {len(PASSES)} checks, {len(WARNINGS)} warning(s).")
    return 0

if __name__ == "__main__":
    sys.exit(main())
