# Phase 3 — Synthetic retail data

This phase owns the foundation the whole demo is grounded on: one internally
consistent year (FY 2025-26) of retail banking data for **Rakesh Sharma
(CTB-RTL-002)**, plus the AI-enriched CRM case narratives the cockpit and the
live-call nudges read from.

It creates **no Azure resources**. It is fast, free and offline.

## Default behaviour: validate the committed pack

`data/csv/` (31 CSVs) and `data/knowledge_base/` are **committed to this
repository**, so every deploy uses byte-identical data and nothing has to be
generated at deploy time. By default this phase only *proves* that pack is
internally consistent and halts the rebuild if anything drifted.

```bash
bash infra/phase3-data/up.sh          # validate the committed pack (default)
python3 infra/phase3-data/validate_seed.py    # same validation, standalone
```

## Opt-in regeneration

`generate_seed.py` is a fixed-seed, standard-library-only, **no-network**
generator for the whole pack. Regeneration is deliberately opt-in so deploys stay
reproducible:

```bash
REGENERATE_SEED=1 bash infra/phase3-data/up.sh                      # Rakesh only
REGENERATE_SEED=1 SEED_CUSTOMERS=3 bash infra/phase3-data/up.sh     # + extra personas

# or drive it directly (e.g. into a scratch directory to inspect first)
python3 infra/phase3-data/generate_seed.py --customers 3 --out /tmp/pack/csv --kb /tmp/pack/kb
python3 infra/phase3-data/generate_seed.py --enrich-only            # narratives + manifest only
```

`--enrich-only` leaves every CSV untouched and rebuilds only
`data/knowledge_base/crm_cases_enriched.csv` and `ai_generation_manifest.json`
from whatever pack is on disk.

### Golden target

With `--customers 1` the generator reproduces the committed pack's shape exactly:

| rows | file |
|-----:|------|
| 516 | `02_accounts/current_account_transactions_fy2025_26.csv` |
| 365 | `02_accounts/daily_balances.csv` |
| 365 | `03_credit/daily_limit_utilization.csv` |
|  30 | `08_rm/rm_daily_activity.csv` |
|  14 | `03_credit/repayment_history.csv` |
|   8 | `02_accounts/counterparty_master.csv` |
|   7 | `05_operations/document_status.csv` |
|   6 | `06_crm/rm_interactions.csv` |
|   5 | `05_operations/service_requests.csv`, `06_crm/engagement_threads.csv` |
|   4 | `06_crm/opportunities.csv`, `06_crm/crm_tasks.csv` |
|   2 | `02_accounts/accounts.csv`, `03_credit/loan_facilities.csv`, `05_operations/cheque_returns.csv` |

Determinism is per-customer: each random stream is derived from
`(seed, customer_id, purpose)`, so **adding a customer never perturbs an existing
one** — Rakesh's 516 transactions are byte-identical at `--customers 1` and
`--customers 4`.

## Personas

Each customer declares a persona in `01_master_data/portfolio_assignments.csv`
(`priority_hint`), and `validate_seed.py` checks the data against it.

| persona | income YoY | EMI bounces | CIBIL | card utilisation |
|---------|-----------|-------------|-------|------------------|
| `stress` | down | ≥ 2 | < 700 | opens 82-92%, revolving into over-limit |
| `stable` | up | 0 | 740-900 | low, paying in full |
| `recovering` | up | ≤ 1 | 660-739 | falling as the balance is paid down |

**Rakesh is preserved exactly.** He is always customer 1, always `stress`, and
always carries every anchor the demo depends on: income 14,40,000 → 11,20,000,
two EMI bounces, CIBIL 642, the disputed ₹48,500 GlobalMart charge, SMA-1 on the
personal loan, and card utilisation opening in the 82-92% band.

## Realism

Rather than a flat random walk, the ledger is shaped by:

- **salary-date clustering** — income lands around the 1st and the 16th (with
  more jitter for a self-employed customer), and discretionary spend clusters in
  the days right after,
- **EMI cadence** — a fixed monthly auto-debit that the repayment history mirrors,
- **festival seasonality** — a Sep-Nov Diwali/wedding build-up, a December bump
  and a slow Feb-Mar quarter-end,
- **an annual spend budget** per persona, so the year closes on a plausible
  balance instead of drifting,
- **eased utilisation curves** with day-to-day noise and anchored endpoints.

## Consistency guarantees (proven by `validate_seed.py`)

1. All 18 required CSVs present and non-empty.
2. Referential integrity: transactions → customer / account / counterparty,
   utilisation → facility.
3. Per-transaction running balances reconcile to within ₹1 when sorted by
   `(txn_timestamp, txn_id)`.
4. Every `cheque_returns` row cross-references a real `is_return=Y` transaction.
5. Every repayment links to an EMI / loan-servicing debit.
6. Rakesh's full stress narrative (see above).
7. Every additional customer is consistent with its declared persona.

## Known, deliberately unfixed

**`rm_interactions.linked_task_id` dangles for two of Rakesh's rows.**
`INT-B-005` and `INT-B-006` reference `TASK-B-005` and `TASK-B-006`, but
`crm_tasks.csv` only defines `TASK-B-001`–`TASK-B-004`. This predates the
generator (it is hand-authored fixture data) and affects **only** CTB-RTL-002 —
`generate_seed.py` clamps the link with `min(i, len(task_defs))`, so every
generated customer is clean.

It is left as-is on purpose. The reference is **inert**: the only occurrence of
`linked_task_id` anywhere in the codebase is a write defaulting it to `""`
(`backend/app/routes/call_records.py`), so nothing dereferences it and nothing
renders it. Both possible fixes cost more than the defect:

- editing Rakesh's rows breaks the invariant that his committed data is
  byte-identical, which the whole append-only design exists to protect;
- adding `TASK-B-005`/`006` would surface two new tasks in his CRM and change
  what is on screen during the demo.

Contrast with the `repayment_history` quoting fix, which *was* applied: that one
corrupted text the cockpit actually displays, and the dropped fragment
(`SMA-1`) is load-bearing for the collections narrative.

`validate_seed.py` does not check `linked_task_id`, so this does not affect its
result. A cross-customer FK isolation sweep over all 7,019 references found zero
leakage between customers.

## Provenance

`data/knowledge_base/ai_generation_manifest.json` is written **by the run** and
records the real generated-at timestamp, seed, customer count, per-file row
counts, the number of enriched cases and the actual validator result — including
any errors or warnings. It is not hand-maintained.
