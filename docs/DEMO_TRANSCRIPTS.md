# Live Call Copilot — Test Transcripts & Demo Script

Two ready-to-use call scripts for testing the live nudge engine. Use them two ways:

- **Live (real Voice Live):** open the copilot, click **Start live call**, put the
  call on speakerphone (or just read both parts aloud near the mic). As each
  customer line is spoken and transcribed, the matching nudge appears on the right.
- **Replay (no mic):** click **Replay test script** — the same lines are sent to
  the nudge engine via `/v1/voice/simulate` and nudges render live. Best for a
  reliable demo or when a mic isn't available.

Each customer line below notes the **nudge it should fire**. RM lines are context
only (they don't trigger nudges — the copilot nudges the RM, not itself).

---

## Call 1 — Aarav Precision Components (CTB-MSME-001) · growth / enhancement

> Pick "Aarav Precision (growth)" in the dropdown.

| # | Speaker  | Line | Expected nudge |
|---|----------|------|----------------|
| 1 | RM       | "Good morning, this is your relationship manager from Contoso Bank. Is now a good time?" | — |
| 2 | Customer | "Yes, please go ahead." | — |
| 3 | Customer | "We have received a larger OEM order and need more working capital before June." | **High / Document** — enhancement is supportable (rising credits, high utilization); ask for GST, stock statement, debtor aging, PO; flag ~42% buyer concentration |
| 4 | Customer | "Our turnover has grown a lot this year." | **Medium / Data point** — confirm growth vs bank credits + GST trend; note concentration |
| 5 | Customer | "Can you just approve the higher limit now?" | **High / Warning** — do NOT commit approval on the call; enhancement needs appraisal; use non-committal language |

**Story:** the copilot supports the growth conversation but enforces the
compliance line — no on-call approval, documents first.

---

## Call 2 — Kaveri Textiles and Traders (CTB-MSME-002) · stress / caution

> Pick "Kaveri Textiles (stress)" in the dropdown.

| # | Speaker  | Line | Expected nudge |
|---|----------|------|----------------|
| 1 | RM       | "Hello, this is Contoso Bank relationship management. May I discuss your account?" | — |
| 2 | Customer | "Yes, okay." | — |
| 3 | Customer | "The cheque bounced because one buyer delayed payment, but we expect funds next week." | **High / Question** — 6 cheque returns + declining credits on record; ask which buyer, amount, date; do NOT allege wrongdoing |
| 4 | Customer | "Why are the bank charges so high?" | **Medium / CRM update** — service recovery first; acknowledge open ticket before commercial topics |
| 5 | Customer | "I already submitted the insurance copy last week." | **High / Document** — insurance shows Expired; don't mark received on call; log a task to verify |
| 6 | Customer | "We may move the account to another bank if this is delayed." | **High / Warning** — attrition risk; acknowledge, confirm issue + timeline, escalate to Branch Manager; create retention task |

**Story:** the copilot keeps the RM risk-first — clarify don't accuse, recover
service before selling, never silently accept a document claim, escalate attrition.

---

## What to point out in a demo

- **Fusion:** each nudge blends what was *said* with the customer's *real data*
  (cheque-return count, utilization, concentration, open tickets, doc status) and
  the relevant **SOP reference** — visible in the evidence chips on each nudge.
- **Compliance:** the "approve now" line triggers a *warning*, not a green light;
  the insurance claim is never auto-accepted. Human-in-the-loop throughout.
- **Approval-gated writes:** click **Accept** on a nudge → it posts a CRM
  update-candidate and approves it (RM-1042). Check the CRM dashboard's Audit
  Trail drawer — the `voice.nudge_fired` and `crm.saved` events are logged.
- **Cross-sell tie-in:** the enhancement nudge for Aarav reflects the same
  eligibility logic as the cross-sell engine; for Kaveri, credit products stay
  suppressed — the copilot won't push lending to a stressed account.

## Tips for the live (mic) path
- Use one device on speakerphone so the mic hears both voices; or read both parts
  yourself. The single-mic demo labels all transcribed speech as "Customer" — that
  is expected for Option A (single-device capture).
- Speak the **bolded trigger phrases** clearly (e.g. "more working capital",
  "cheque bounced", "another bank") — these are what the intent matcher keys on.
- If transcription renders an English word oddly, the nudge may still fire (the
  matcher is tolerant); if it doesn't, just say the line again or use Replay.
