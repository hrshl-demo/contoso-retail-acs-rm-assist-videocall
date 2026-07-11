# Video Assist — Entra edition (no keys)

Customer-only video app ↔ RM on Teams. AI co-pilot runs in the background and delivers the
**customer synopsis** and **real-time nudges** to the **RM's Teams chat only** (never the
customer UI). Azure AI is accessed via **Entra ID / managed identity** — no API keys anywhere.

AI config: endpoint `…/openai/v1`, deployment `${AOAI_CHAT_DEPLOYMENT_NAME}` (the chat
deployment created by phase2), bearer token via `DefaultAzureCredential` with scope
`https://ai.azure.com/.default`.

## Deploy

```bash
tar -xzf videoassist-entra.tar.gz
cd videoassist-entra
chmod +x *.sh

./deploy.sh                                # build + deploy; assigns a managed identity + AI role
./set-teams-webhook.sh "<workflow-url>"    # point nudges at the RM's Teams chat
```

The deploy gives the Container App a **system-assigned managed identity** and tries to grant it
**"Cognitive Services OpenAI User"** on the AI Foundry account (created by phase2). If your account
can't assign roles, it prints the exact command for an admin (or run `./configure-ai.sh` once you
have rights).

### Verify (important order)
- `https://<app>/healthz` → `aiReady:true` means endpoint+identity are configured.
- `https://<app>/diag`   → **`{"ok":true,...}`** means the Entra token actually works against the
  model. If it shows `403`, the role isn't assigned/propagated yet (wait a few min or run
  `./configure-ai.sh`). If `404`, the deployment name is wrong — fix `AZURE_AI_CHAT_DEPLOYMENT`.

## RM's Teams nudge channel (no admin rights)

1. Teams → a chat/channel → **··· → Workflows** → template **"Post to a channel when a webhook
   request is received."**
2. Save, copy the **HTTP POST URL**, run `./set-teams-webhook.sh "<url>"`.

## Demo

### A · Customer mobile app — one-tap instant call (recommended)
1. Open `/bank?customer_id=CTB-RTL-002` (the RM can launch/share it from CRM Step 7).
2. The customer taps **Video call your RM**. The RM-side Teams meeting link is created
   automatically (see *Meeting provisioning* below) and a **meeting request posts to the
   RM's Teams** — the customer never sees a link.
3. A ~`CALL_LEAD_SECONDS` (default 60s) countdown runs; then a **Join call** button appears.
4. Tapping it hands off to the call app with `?booking=<id>` and auto-joins **the same** Teams
   meeting the RM has — synopsis + nudges post to the RM's Teams as usual.

#### Meeting provisioning (how the RM link is generated) — pick one
The link the RM opens and the link the customer joins are **the same** `booking.meetingLink`;
the only question is who creates the real Teams meeting. `provisionMeetingLink()` tries, in order:
1. **`SCHEDULE_WEBHOOK_URL`** — an RM-owned Power Automate flow ("Create event"/"Create a Teams
   meeting"). Real meeting **+ RM calendar event**, no app registration or admin consent.
2. **Microsoft Graph (app-only)** — set `GRAPH_TENANT_ID` / `GRAPH_CLIENT_ID` /
   `GRAPH_CLIENT_SECRET` (app with the **Calendars.ReadWrite** application permission,
   admin-consented) and `RM_USER_ID` (the RM's mailbox UPN/objectId). Each tap creates a fresh
   **calendar event on the RM's calendar** with a Teams meeting attached. This is the production path.
   **Fully automated setup:** run `bash setup-graph.sh` (see below) — it creates the app registration,
   grants admin consent, mints the secret, and writes `infra/common/secrets.env`; no portal clicks.
3. **`RM_MEETING_URL`** — the RM's standing Teams meeting join link (2-min setup: Teams →
   Calendar → New meeting → save → copy the join link). Real & joinable, but reuses one meeting
   (no per-call calendar event). Easiest way to make the demo link **actually open** today.
4. **synthetic** — `…/meetup-join/DEMO-<id>`. Only if none of the above are set; this link will
   **not** open a real meeting and is for offline UI walkthroughs only.

> To make the RM link open a real Teams meeting, set **one** of the above. For a quick real demo
> use `RM_MEETING_URL`; for per-call calendar entries use Graph or the Power Automate flow.

#### Fully-automated Graph setup (`setup-graph.sh`)
For the zero-manual, calendared production path, run once from the repo root:
```bash
RM_UPN=priya.nair@contoso.com bash setup-graph.sh     # the RM's real mailbox
bash build.sh --type=ptu                               # (or --type=payg) picks up the creds
```
`setup-graph.sh` (all via `az`, no portal clicks): creates the Entra app registration + service
principal, adds **Microsoft Graph · Calendars.ReadWrite (Application)**, grants **tenant admin
consent**, mints a client secret, and writes `infra/common/secrets.env` (git-ignored, auto-sourced
by `env.sh`). Idempotent — re-run to rotate the secret.

Requirements: `az login` as an identity that can create app registrations **and** grant admin
consent (Global Administrator or Privileged Role Administrator). Admin consent for an application
permission always needs an admin — the script performs it for you, but the signed-in account must
hold the privilege. The RM mailbox must be Exchange Online-licensed.

Optional hardening (recommended for real tenants): `Calendars.ReadWrite` (Application) lets the app
write to *any* mailbox. Scope it to just the RM(s) with an Exchange Online **Application Access
Policy** (`New-ApplicationAccessPolicy`, run in Exchange Online PowerShell — separate from `az`).



### B · Manual / RM-hosted meeting
1. RM (Teams): Meet now → Start meeting → People → Copy join link.
2. Customer: open app URL → paste link → Start video session → allow camera/mic.
3. RM: admit from lobby → **synopsis posts to Teams**.
4. Customer speaks (Chrome/Edge) → **nudges post to Teams** (~1/12s). Append **`?debug=1`** to the
   URL for a "simulate a line" box to demo deterministically.

## Tear down (no prompts; AI safe)
```bash
./teardown.sh             # app, env, registry, workspace; keeps ACS + all AI
./teardown.sh --with-acs  # also remove ACS
```
Refuses anything matching ontology/openai/foundry/cognitive — your AI resources can't be deleted.

## Notes
- Embeddings: if `text-embedding-3-small` isn't the exact deployment name, RAG silently falls back
  to full-portfolio grounding (synopsis/nudges still work). Set `AZURE_AI_EMBED_DEPLOYMENT` if you
  want vector retrieval and the name differs.
- Container logs for debugging: `az containerapp logs show -n videoassist-web -g "$AZ_RG" --follow`.
