#!/usr/bin/env bash
# infra/phase2-ai/grant-acs-cognitive-role.sh
#
# TRANSCRIPTION HOTFIX (v0.14.4)
#
# Root cause of "transcription stuck at starting / no transcript stream":
# ACS Call Automation real-time transcription requires that the *ACS resource's
# own managed identity* has the "Cognitive Services User" role on the Azure AI
# Services (Cognitive Services) account it calls for speech. The phase2-ai bicep
# only granted that role to the app's user-assigned managed identity (UAMI), not
# to ACS itself. So ACS accepts the transcription config at call setup
# (transcription_configured_at_setup=true) but silently cannot invoke speech,
# and you never receive TranscriptionStarted / TranscriptionData.
#
# This script:
#   1. Enables a system-assigned managed identity on the ACS resource (idempotent).
#   2. Grants that identity "Cognitive Services User" on the AI Foundry account
#      ($EXISTING_AISERVICES_NAME, created by phase2).
#
# It is NON-DESTRUCTIVE. It creates nothing that the wipe targets, deletes nothing,
# and does NOT touch the purchased phone number. Safe to re-run.
set -euo pipefail
PHASE="phase2-ai"; export PHASE
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../common/env.sh
source "$SCRIPT_DIR/../common/env.sh"

ensure_az_login
ensure_rg

COGNITIVE_SERVICES_USER_ROLE="a97b65f3-24c7-4388-baec-2e87135dc908"  # built-in role id

log "Resolving ACS resource: $NAME_ACS"
ACS_ID="$(az communication list -g "$AZ_RG" --query "[?name=='${NAME_ACS}'].id | [0]" -o tsv 2>/dev/null || true)"
[[ -n "$ACS_ID" ]] || die "ACS resource $NAME_ACS not found in $AZ_RG. Run phase2-ai/up.sh first."

log "Enabling system-assigned managed identity on ACS (additive, idempotent)"
# IMPORTANT: only use the additive 'identity assign' form. Do NOT use
# 'az resource update --set identity.type=...' — that OVERWRITES the identity
# block and can strip an existing user-assigned identity, breaking the backend's
# ability to authenticate to ACS.
az communication identity assign --name "$NAME_ACS" -g "$AZ_RG" --system-assigned >/dev/null 2>&1 || true
ACS_PRINCIPAL_ID="$(az resource show --ids "$ACS_ID" --query "identity.principalId" -o tsv 2>/dev/null || true)"
[[ -n "$ACS_PRINCIPAL_ID" && "$ACS_PRINCIPAL_ID" != "null" ]] \
  || die "Could not read ACS system-assigned principalId. Enable a system-assigned identity on ACS in the portal (Settings > Identity), then re-run. Do NOT remove any existing user-assigned identity."
ok "ACS managed identity principalId: $ACS_PRINCIPAL_ID"

log "Resolving AIServices (Cognitive Services) account: $EXISTING_AISERVICES_NAME"
AISVC_ID="$(az cognitiveservices account show -n "$EXISTING_AISERVICES_NAME" -g "$AZ_RG" --query id -o tsv 2>/dev/null || true)"
[[ -n "$AISVC_ID" ]] || die "AIServices account $EXISTING_AISERVICES_NAME not found in $AZ_RG."

log "Granting 'Cognitive Services User' to ACS identity on AIServices (idempotent)"
if az role assignment list --assignee "$ACS_PRINCIPAL_ID" --scope "$AISVC_ID" \
      --query "[?roleDefinitionName=='Cognitive Services User'] | [0]" -o tsv 2>/dev/null | grep -q .; then
  ok "Role already assigned. Nothing to do."
else
  az role assignment create \
    --assignee-object-id "$ACS_PRINCIPAL_ID" \
    --assignee-principal-type ServicePrincipal \
    --role "$COGNITIVE_SERVICES_USER_ROLE" \
    --scope "$AISVC_ID" >/dev/null
  ok "Role assigned: Cognitive Services User (ACS -> $EXISTING_AISERVICES_NAME)"
fi

cat <<EOF

$(printf '\033[1;32m[✓] ACS -> Cognitive Services linkage complete.\033[0m')

Role assignments can take 1-5 minutes to propagate. Then:
  1. Start a FRESH ACS call from the dashboard (existing calls won't pick up the new role).
  2. Watch GET /v1/acs/sessions/{id}/events for:
       acs.transcription.metadata   (TranscriptionMetadata = speech backend connected)
       transcript.final             (real transcript lines)
  3. If you still see TranscriptionFailed with code 8581, the issue is the WS
     transport URL reachability (check ACS_PUBLIC_BASE_URL), not the role.
EOF
