# Contoso Bank — Core Banking + CRM Console

A **zero-dependency static SPA** that renders the synthetic Contoso Bank dataset
(`data/contosobank/contosobank_dataset.json`) the way an Indian bank's staff console would — a
**Finacle-style core-banking view fused with a Dynamics / CRM-Next-style CRM**, styled after an
IndusInd-like private/commercial bank.

## What it shows

A tab per **Business Unit**:

| Tab | Segment | Contents |
|-----|---------|----------|
| **Enterprise Overview** | all | bank-wide KPIs (deposits, advances, AUM, pipeline, tickets, covenant breaches), per-BU relationship value, branch network, product catalogue |
| **Retail Banking** | `RETAIL` | RM cockpit + customer list |
| **Business Banking** | `MSME` | RM cockpit + customer list |
| **Corporate & Institutional** | `CORPORATE` | RM cockpit + customer list |

Selecting a customer opens a **360° view** with sub-tabs (rendered only when data exists):

- **Overview** — profile, KYC & risk, contacts, bureau/FOIR, advisor brief, latest relationship event
- **Accounts** — CASA/deposit accounts, balances, IFSC, status (core-banking style)
- **Transactions** — full ledger with rail (UPI/NEFT/RTGS), Dr/Cr, running balance; filter by account
- **Credit & Facilities** — loans, working-capital facilities (with utilisation), covenants (PASS/FAIL,
  thresholds, observed values), collateral, repayment schedule
- **Investments** — MF/third-party holdings, NAV, market value, SIPs, suitability
- **Trade Finance** — LC/BG events
- **CRM 360** — opportunities (with pipeline stages), email threads, interaction & meeting timeline
- **Service** — service tickets & complaints (SLA breach, priority, reopen count)
- **Documents** — document vault
- **Relationship Arc** — the 6-month narrative arc

Currency is rendered in Indian format (lakh/crore, e.g. `₹1.23 Cr`). All data is **synthetic**.

## Run locally

It's a static site; serve the **repository root** so the console can reach the dataset:

```bash
python3 -m http.server 8791
# then open:
#   http://127.0.0.1:8791/corebank-console/index.html?data=/data/contosobank/contosobank_dataset.json
```

The `?data=` query param overrides the dataset URL (default: `./data/contosobank_dataset.json`,
relative to the console — which is where it lives when deployed on the VM).

## How it's deployed

During a build, `tools/deploy-console-on-vm.sh` syncs this folder plus the generated dataset into the
phase-10 VM's Caddy webroot (`/opt/rmx/web/`), so it is served over the reusable Let's Encrypt TLS host
at `https://rmassist.<ip>.nip.io/`. Skip with `SKIP_CONSOLE=1`.

## Files

```
index.html            app shell (header, BU tab bar, content mount)
assets/styles.css     maroon Indian-bank theme (cards, tables, badges, timelines)
assets/app.js         vanilla JS: fetch dataset, render all views (no build step, no deps)
```
