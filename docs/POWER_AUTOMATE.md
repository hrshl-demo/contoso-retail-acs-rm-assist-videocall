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
> chat or channel** and build the card from `text`. The app already sends HTML
> that renders well as a message; a card is a cosmetic upgrade.

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

> **Security:** the webhook URL contains a signature (`sig=`) and is effectively a
> credential — anyone with it can post to your flow. Keep it out of git (it is not
> committed in this repo) and rotate it by recreating the flow trigger if leaked.
