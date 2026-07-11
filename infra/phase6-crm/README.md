# Phase 6 — CRM Dashboard (RM Cockpit)

The visible hero surface: a single-page RM cockpit served by unprivileged nginx,
talking to the Phase 4 Tool API. Institutional dark theme (Fraunces + IBM Plex).

## What it shows
- Portfolio priority queue (sidebar) — Risk Watch / Growth / Renewal Due buckets
- Customer 360 — conduct metrics, monthly-credit chart, facility
- Early Warning Signals — severity-coded, each with its false-positive guardrail
- Documents & next-best questions
- One-click renewal memo draft (evidence-cited, with the non-approval disclaimer)
- Approval-gated CRM write (propose -> confirm -> save), proving human-in-the-loop
- Grounded policy chat (calls /v1/rag/retrieve; says "not found" rather than guessing)
- Audit Trail drawer — the glass box; every AI action logged

## Config injection
TOOLAPI_URL and the bearer token are injected into index.html at container
startup (nginx /docker-entrypoint.d). Bearer comes from KV via the UAMI.

## Run / teardown
    bash infra/phase6-crm/up.sh     # build + deploy; prints the dashboard URL
    bash infra/phase6-crm/down.sh   # tag-guarded delete

## Note (POC scope)
The bearer is injected into browser-delivered JS for demo simplicity. For a
hardened build this should move behind a server-side session/proxy (Phase 7
territory). Flagged, not hidden.

## Phase 6.5 additions (start-of-day briefing + cross-sell)
- Backend (ships in the Phase 4 Tool API image):
  - `/v1/briefing/daily` and `/v1/briefing/customer/{id}` — per-customer narrative
    with a structured reasoning trace on every line (drill-down "why").
  - `/v1/customers/{id}/cross-sell` — eligibility-checked cross-sell/upsell engine
    (blocking signals veto credit products for stressed customers).
- Frontend: "Today's Briefing" is the landing view (click any line to expand its
  evidence); Customer 360 gains a Cross-sell/Upsell panel.
- Data: `data/knowledge_base/product_catalog.csv` (deterministic) added in Phase 3.

NOTE: because the briefing/cross-sell code lives in the Tool API, deploying these
updates = rebuild the Phase 4 image (rebuild-all.sh does this automatically; or
re-run phase4-toolapi/up.sh then phase6-crm/up.sh).
