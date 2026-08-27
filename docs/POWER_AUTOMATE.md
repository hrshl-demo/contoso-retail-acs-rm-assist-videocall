# Power Automate / Power Platform: Teams nudges & scheduling

> **This demo sends NO email.** The meeting is a *direct meeting request*: the
> RM-side Teams meeting is created straight on the RM's calendar via Microsoft
> Graph (or an optional RM-owned scheduling flow) and the customer joins in-app.
> There is **no email confirmation and no Email Communication Service**. If you
> previously built a Power Automate "email automation" flow (e.g. *HNI RM Assist
> Email Automation*), **turn it off / delete it** — it is not part of this repo and
> premium Power Automate email connectors are chargeable.

The demo's Azure stack is fully self-contained, but **two integrations live in
Power Platform** because they cannot be created with Azure IaC:

1. **Teams nudges** — the live call posts an AI **synopsis**, **nudges** and a
   **"video call requested" card** into the RM's Teams. This goes through a
   Power Automate flow exposed as a signed webhook (`TEAMS_WEBHOOK_URL`).
2. **Scheduling (optional)** — an optional RM-owned flow that, on a booking,
   creates an Outlook calendar event and returns the real Teams `joinUrl`
   (`SCHEDULE_WEBHOOK_URL`). This flow must **not** send any email. If it is left
   unset, the app creates the meeting directly via Microsoft Graph instead.

Neither is required for the video call to work — set them when you want Teams
posting and/or a real calendar invite. **Do not wire any email step into either.**

---

## The contract the app expects

The app (`videoassist/teams.js`) does a single `POST` with a tiny JSON body:

```http
POST <TEAMS_WEBHOOK_URL>
Content-Type: application/json
X-Contoso-Event-Id: <opaque id>        (optional, for de-duplication)
X-Contoso-Message-Kind: nudge|synopsis|call-request   (optional)

{ "text": "<b>💡 Nudge · UPSELL</b><br>Offer the top-up loan…" }
```

- `text` is **light HTML** (`<b>`, `<br>`, `<a href>`, `<i>`) — the Teams *Post
  message* action renders HTML, so map the flow's message to
  `triggerBody()?['text']`.
- A response is optional for nudges. For the **scheduling** flow, respond with
  JSON containing the meeting join link (see below).

---

## "Open in RM Cockpit" deep links — **no flow changes needed**

Every AI card (nudge, answer, synopsis, consent-gated case) now ends with a line
like:

```html
<b>🔎 <a href="https://rmassist.&lt;static-ip&gt;.nip.io/?customer=CTB-RTL-002&focus=VCALL-AB12CD34%3Aturn-7%3Anudge&kind=live_nudge">Open this nudge in RM Cockpit</a></b> — full evidence, runtime trace and drill-down.
```

**This is just more HTML inside the existing `text` field.** If your flow already
maps *Message* to `triggerBody()?['text']`, the link appears and works with
**zero changes** — nothing to re-import, re-authorise or re-publish.

Clicking it opens the CRM cockpit, selects that customer and opens the contextual
detail drawer on that exact insight: the full text, the suggested talk-track, the
internal policy basis, the AI runtime trace (tool, records scanned, latency,
confidence, model) and the consent state — with drill-down buttons to the leaf
rows on file and to the matched SOP clauses.

The link resolves against `GET /insights/:eventId` on the video-call app, where
`eventId` is the same value already sent in the `X-Contoso-Event-Id` header. The
cockpit also holds an SSE subscription (`GET /insights/stream`) so an open tab has
usually cached the payload **before** the RM clicks, making the drawer instant.

- The cockpit base URL is the VM's own origin, `https://<rmassist-host>`, injected as
  `CRM_BASE_URL` by `tools/deploy-videoassist-on-vm.sh`. The cockpit and the call app are
  the SAME origin now (Caddy serves the cockpit at `/` and the call app at `/video`), so
  the deep link never crosses a hostname. Override by exporting `CRM_BASE_URL` before the
  deploy.
- If `CRM_BASE_URL` is unset (e.g. local dev), the extra line is simply omitted
  and every card is byte-identical to before.
- The insight buffer is in-memory and bounded (`INSIGHT_STORE_MAX`, default 400),
  so a link from a long-past call may report "no longer in the live call buffer".
  The durable copy is always the call record written at `/session/finalize`.

### Optional upgrade path: Adaptive Cards

The POST body now *may* also contain a `card` field:

```jsonc
{
  "text": "<b>💡 Nudge · RETENTION</b><br>…",   // unchanged contract
  "card": { "type": "AdaptiveCard", "version": "1.4", "body": [ … ],
            "actions": [ { "type": "Action.OpenUrl", "title": "Open in RM Cockpit", "url": "…" } ] },
  "deepLink": "https://rmassist.<static-ip>.nip.io/?customer=…&focus=…"
}
```

`card` and `deepLink` are **purely additive**. A flow whose trigger schema only
declares `text` never sees them, so **existing flows are unaffected** — this is
deliberately not a hard switch to Adaptive Cards.

To opt in later:

1. Edit the flow trigger's *Request Body JSON Schema* to add
   `"card": { "type": "object" }`.
2. Replace **Post message in a chat or channel** with **Post card in a chat or
   channel**.
3. Set *Adaptive Card* to `triggerBody()?['card']`.

Keep the HTML path as your fallback: if the card action ever fails to render, the
same content is still present in `text`.

---

## 1) Teams nudge flow (`TEAMS_WEBHOOK_URL`)

Create it once; it produces a stable signed URL.

1. **[Power Automate](https://make.powerautomate.com)** → **Create** → **Instant
   cloud flow** → trigger **"When a Teams webhook request is received"**
   (or **"When an HTTP request is received"** if you prefer a raw HTTP trigger).
   - Request body JSON schema:
     ```json
     { "type": "object", "properties": { "text": { "type": "string" } } }
     ```
2. Add action **Microsoft Teams → Post message in a chat or channel**
   (post as *Flow bot*, to the RM's chat or a "RM Assist" channel).
   - **Message** = dynamic content `text` (`triggerBody()?['text']`).
3. **Save.** Open the trigger card and **copy the HTTP POST URL** — that whole
   signed URL is your `TEAMS_WEBHOOK_URL`.

Optionally build a second, identical flow for live nudges only and use it as
`TEAMS_NUDGE_WEBHOOK_URL` (the app falls back to `TEAMS_WEBHOOK_URL` if unset).

> **Adaptive Card instead of plain HTML?** Replace step 2 with **Post card in a
> chat or channel** and bind it to `triggerBody()?['card']`, which the app now
> sends alongside `text`. See *"Optional upgrade path: Adaptive Cards"* above.
> The HTML message already renders well, so this is a cosmetic upgrade.

---

## 2) Scheduling + email flow (`SCHEDULE_WEBHOOK_URL`, optional)

Use this if you want the booking to produce a **real Outlook event + emailed
invite** and hand the app a real Teams join link — the no-Graph alternative to
`setup-graph.sh`.

1. Power Automate → **Instant cloud flow** → **"When an HTTP request is
   received"** (this trigger is a **premium** connector).
2. **Office 365 Outlook → Create event (V4)** on the RM's calendar with
   **Is online meeting = Yes** (Teams). Add the customer as a required attendee to
   trigger the invite email automatically.
3. *(Optional)* **Office 365 Outlook → Send an email (V2)** — a branded
   confirmation to the customer/RM. (Or use **Azure Communication Services Email**,
   which this stack already provisions — see below.)
4. **Response** action returning the meeting link:
   ```json
   { "joinUrl": "@{outputs('Create_event')?['body/onlineMeeting/joinUrl']}" }
   ```
5. Copy the trigger URL → `SCHEDULE_WEBHOOK_URL`.

The app reads `joinUrl` (also accepts `joinLink`/`meetingUrl`) and uses it as the
RM's link; the customer joins the **same** meeting via ACS interop and never sees
the URL.

### Email

**Intentionally none.** This demo never sends email — not from the backend and
not from a flow. The customer is added to the RM's live Teams meeting in-app via
ACS interop, and the RM sees the meeting on their own calendar (created silently
with no attendees, so Exchange raises no invitation email). Do **not** add an
"Send an email" / ACS Email step anywhere in the flow.

---

## Where these values are configured

All four are plain environment variables with **empty defaults** in
[`infra/common/env.sh`](../infra/common/env.sh); set them in **one** of these
places (do **not** commit real signed URLs):

| Where | How | Best for |
|-------|-----|----------|
| `infra/common/secrets.env` | copy `secrets.env.example`, fill in, it's git-ignored | local builds on your VM |
| GitHub repository secrets | `gh secret set TEAMS_WEBHOOK_URL -R <owner/repo> -b "<url>"` | CI/CD deploys (see [CICD.md](CICD.md)) |
| Inline for one run | `TEAMS_WEBHOOK_URL="…" bash build.sh` | quick one-off |

| Variable | Purpose |
|----------|---------|
| `TEAMS_WEBHOOK_URL` | Teams posting for synopsis + nudges + call-request card |
| `TEAMS_NUDGE_WEBHOOK_URL` | optional dedicated live-nudge flow (falls back to `TEAMS_WEBHOOK_URL`) |
| `SCHEDULE_WEBHOOK_URL` | optional real booking → Outlook event/email → returns `joinUrl` |
| `SCHEDULE_AVAILABILITY_WEBHOOK_URL` | optional real availability lookup for the Step-7 page |
| `CRM_BASE_URL` | optional override for the "Open in RM Cockpit" deep-link base (defaults to the VM's own origin, `https://<rmassist-host>`) |
| `INSIGHT_STORE_MAX` | optional size of the in-memory insight ring buffer (default 400) |

> **Security:** the webhook URL contains a signature (`sig=`) and is effectively a
> credential — anyone with it can post to your flow. Keep it out of git (it is not
> committed in this repo) and rotate it by recreating the flow trigger if leaked.
