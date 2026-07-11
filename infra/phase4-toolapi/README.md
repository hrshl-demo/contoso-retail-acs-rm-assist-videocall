# Phase 4 — Tool API (the analytical brain)

The FastAPI Tool API Container App. Loads the synthetic CSV pack into memory at
startup (swappable `DataStore` seam → Azure SQL later) and exposes the RM Assist
analysis + approval-gated CRM API.

## Storage decision
In-memory (pandas/dicts), data baked into the image. No SQLite, no external DB:
the dataset is read-only, fits in RAM, and resets on restart — ideal for a POC.
`DataStore` is the single seam; swap its implementation to move to Azure SQL
without touching any caller.

## Engines (deterministic, explainable — no LLM)
- `analytics.AccountConduct` — 12-month conduct metrics (UC3)
- `analytics.EWSEngine` — early-warning signals with severity + false-positive guardrails (UC6)
- `analytics.EnhancementAssessor` — enhancement eligibility, hard-gated (UC5)
- `memo.MemoService` — evidence-cited renewal/enhancement draft, never an approval (UC4)
- `portfolio` — priority queue (UC1) + customer 360 (UC2)

## API (bearer-protected, blueprint 12.5)
GET  /v1/portfolio/priority-queue
GET  /v1/customers/{id}/360
POST /v1/analysis/account-conduct
GET  /v1/customers/{id}/ews
GET  /v1/customers/{id}/enhancement
POST /v1/memo/renewal-draft
POST /v1/crm/update-candidate   (refuses credit-approval writes; 422)
POST /v1/crm/approve-update     (human-in-the-loop)
GET  /v1/crm/pending
GET  /v1/audit/events           (glass-box stream)

## Run
    bash infra/phase4-toolapi/up.sh    # ACR build + deploy + smoke test
    bash infra/phase4-toolapi/down.sh  # tag-guarded teardown

## Proven (local TestClient)
Priority queue ranks Kaveri=Risk Watch, Aarav=Growth; memo refuses Kaveri
enhancement citing critical blockers; CRM guardrail blocks "approved" writes;
approve→audit chain works.
