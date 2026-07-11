# Phase 9 — Video Assist (Step 7 video call)

Builds and deploys the **Video Assist** app (`videoassist-web`) — the customer-facing
video call that joins the RM's Microsoft Teams meeting via Azure Communication
Services. This is **Step 7** of the merged RM Assist journey, replacing the former
ACS PSTN phone call.

## What it creates / reuses

**Creates (tagged `project=contoso-msme-rm-assist`):**
- `videoassist-web` Container App (Node/Express, port 3000, external HTTPS, min-replicas 1).
- `acs-rm-copilot` ACS resource for video tokens — only if missing (no purchased PSTN number).

**Reuses (does NOT recreate):**
- Contoso ACR (`acrmsme<suffix>`) — the image is built here.
- User-assigned identity `id-msme-app` — already holds `Cognitive Services User`,
  `Cognitive Services OpenAI User` and `Azure AI User`, which is exactly what Video
  Assist needs for chat, embeddings and Speech. No new role assignments.
- Container Apps environment `cae-msme` — hosts the app alongside the other three.
- Key Vault `kv-msme-<suffix>` — the shared Tool API bearer is read from here.
- The AI Foundry account — created by phase2, deleted only by the whole-RG wipe.

## Grounding (the integration boundary)

Video Assist grounds the in-call **synopsis** and **nudges** on the real MSME data by
calling the Contoso Tool API server-side:
- `TOOLAPI_URL` (from phase4 outputs) + `TOOLAPI_BEARER` (Key Vault) are injected as
  env/secret. The bearer is **server-side only** — never sent to the customer browser.
- An AI intent router classifies each utterance and routes factual questions to
  `/v1/customers/{id}/transactions/recent` etc., and advisory moments (e.g. a limit
  request) to `/v1/customers/{id}/enhancement` + SOP retrieval — so the two never
  share evidence (fixes the "everything returns transactions" bug).

## Teams / Power Automate (preserved as-is)

The synopsis and nudges post to the RM's Teams chat via the existing Power Automate
webhook. This phase **never wipes** that secret: deploys are additive
(`az containerapp create/update`, not a full-replace Bicep). Configure it once:

```bash
cd <repo>/videoassist && ./set-teams-webhook.sh "<workflow-webhook-url>"
# or export TEAMS_WEBHOOK_URL=... before running this phase
```

## Run

Driven by the orchestrators (Wave 4 of `infra/rebuild-parallel.sh`):

```bash
bash infra/phase9-videoassist/up.sh      # build + deploy
bash infra/phase9-videoassist/down.sh    # delete videoassist-web (ACS preserved)
ACS_FORCE_DELETE=1 bash infra/phase9-videoassist/down.sh   # also remove acs-rm-copilot
```

Outputs: `outputs.env` → `VIDEOASSIST_URL`, consumed by **phase6-crm** to wire the
Step 7 capstone button (`{VIDEOASSIST_URL}/?customer_id=CTB-MSME-001|002`).

## Verify

```bash
source infra/phase9-videoassist/outputs.env
curl -fsS "$VIDEOASSIST_URL/healthz"   # { ok, aiReady, grounding:true, teamsConfigured:true }
curl -fsS "$VIDEOASSIST_URL/diag"      # Entra auth + Tool API reachability
```

## v1m semantic voice intelligence

The call transport is unchanged. Customer utterances are interpreted by a Foundry
semantic query planner and executed against deterministic Tool API operations. The
voice planner defaults to the created chat deployment (`AOAI_CHAT_DEPLOYMENT_NAME`);
override with `VOICE_AI_CHAT_DEPLOYMENT`. Completed calls create downloadable call
records in the customer's Core CRM record.
