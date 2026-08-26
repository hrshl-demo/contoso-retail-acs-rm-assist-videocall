#!/usr/bin/env bash
# infra/phase9-videoassist/up.sh — Phase 9: build + deploy the Video Assist app
# (Step 7 live video call) into the shared Contoso platform.
#
# Reuse-first: creates ONLY the Video Assist Container App ($NAME_CA_VIDEOASSIST) and a
# dedicated video-token ACS resource ($NAME_ACS_VIDEO, no purchased PSTN number). Reuses
# the shared ACR, the user-assigned identity, and the Container Apps environment from
# phase1, and grounds the in-call synopsis/nudges on phase4's Tool API.
#
# All configuration comes from infra/common/env.sh — nothing here is hardcoded.
#
# Deploys IMPERATIVELY and ADDITIVELY (az containerapp create/update) so a redeploy does
# NOT wipe the server-side Teams webhook secret. The Teams / Power Automate webhook is
# read from TEAMS_WEBHOOK_URL (env.sh) and stored as the `teams-webhook` secret.
set -euo pipefail
PHASE="phase9"; export PHASE
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=../common/env.sh
source "$SCRIPT_DIR/../common/env.sh"

log "Phase 9 — Video Assist (Step 7 live video call)"
ensure_az_login
ensure_rg

VA_APP="$NAME_CA_VIDEOASSIST"
VA_IMAGE_REPO="videoassist"
ACS_NAME="$NAME_ACS_VIDEO"

# Prior-phase outputs: ACR + UAMI (phase1), Tool API URL (phase4).
PHASE1_OUT="$SCRIPT_DIR/../phase1-platform/outputs.env"
PHASE4_OUT="$SCRIPT_DIR/../phase4-toolapi/outputs.env"
for f in "$PHASE1_OUT" "$PHASE4_OUT"; do
  [[ -f "$f" ]] || die "Missing outputs: $f (run that phase first)"
  # shellcheck disable=SC1090
  source "$f"
done
[[ -n "${ACR_LOGIN_SERVER:-}" && -n "${UAMI_ID:-}" && -n "${UAMI_CLIENT_ID:-}" && -n "${TOOLAPI_URL:-}" ]] \
  || die "Incomplete prior outputs (need ACR_LOGIN_SERVER, UAMI_ID, UAMI_CLIENT_ID, TOOLAPI_URL)."
ok "Loaded phase 1 + 4 outputs (TOOLAPI_URL=$TOOLAPI_URL)"

# --- Video Assist ACS resource (video tokens; no purchased number) ------------
log "Ensuring ACS resource '$ACS_NAME' (video calling) ..."
if ! az communication show -n "$ACS_NAME" -g "$AZ_RG" -o none 2>/dev/null; then
  az extension add --name communication --upgrade -y 2>/dev/null || true
  az communication create -n "$ACS_NAME" -g "$AZ_RG" --location Global \
    --data-location "${ACS_DATA_LOCATION:-United States}" -o none
  ok "Created ACS $ACS_NAME"
else
  ok "ACS $ACS_NAME exists"
fi
# Tag for teardown guard + inventory.
ACS_ID="$(az communication show -n "$ACS_NAME" -g "$AZ_RG" --query id -o tsv 2>/dev/null || true)"
[[ -n "$ACS_ID" ]] && az resource tag --ids "$ACS_ID" --tags "$PROJECT_TAG" phase=phase9 -o none 2>/dev/null || true
CONN="$(az communication list-key -n "$ACS_NAME" -g "$AZ_RG" --query primaryConnectionString -o tsv)"
[[ -n "$CONN" ]] || die "Empty ACS connection string for $ACS_NAME."

# --- Shared Tool API bearer (from phase4 outputs.env; Key Vault-free) ---------
TOOLAPI_BEARER="${TOOLAPI_BEARER_TOKEN:-}"
[[ -n "$TOOLAPI_BEARER" ]] || die "TOOLAPI_BEARER_TOKEN missing from phase4 outputs.env (run phase4 first)."

# --- Build the Video Assist image into the shared Contoso ACR (or reuse a pre-build) --
PREBUILT_ENV="$SCRIPT_DIR/../common/prebuilt_images.env"
[[ -f "$PREBUILT_ENV" ]] && source "$PREBUILT_ENV"
if [[ -n "${PREBUILT_VIDEOASSIST_IMAGE:-}" ]]; then
  IMAGE_REF="$PREBUILT_VIDEOASSIST_IMAGE"
  ok "Reusing pre-built Video Assist image (built in parallel during phase2): $IMAGE_REF"
else
  IMAGE_TAG="v$(date -u +%Y%m%d%H%M%S)"
  IMAGE_REF="${ACR_LOGIN_SERVER}/${VA_IMAGE_REPO}:${IMAGE_TAG}"
  log "Building Video Assist image: $IMAGE_REF"
  az acr build --registry "$NAME_ACR" \
    --image "${VA_IMAGE_REPO}:${IMAGE_TAG}" --image "${VA_IMAGE_REPO}:latest" \
    "$REPO_ROOT/videoassist" --only-show-errors \
    || die "ACR build failed (often transient — re-run)."
  ok "Image built"
fi

# --- Secrets + env (Entra AI via the shared UAMI; NO keys) -------------------
SECRETS=( "acs-conn=$CONN" "toolapi-bearer=$TOOLAPI_BEARER" )
ENVVARS=( "ACS_CONNECTION_STRING=secretref:acs-conn"
          "AZURE_CLIENT_ID=$UAMI_CLIENT_ID"
          "AZURE_AI_ENDPOINT=$AZURE_AI_ENDPOINT"
          "AZURE_AI_CHAT_DEPLOYMENT=$AZURE_AI_CHAT_DEPLOYMENT"
          "VOICE_AI_CHAT_DEPLOYMENT=$VOICE_AI_CHAT_DEPLOYMENT"
          "VOICE_AI_FAST_DEPLOYMENT=$VOICE_AI_FAST_DEPLOYMENT"
          "VOICE_AI_REASONING_EFFORT=$VOICE_AI_REASONING_EFFORT"
          "AI_REASONING_DEPLOYMENTS=${AI_REASONING_DEPLOYMENTS:-}"
          "VOICE_AI_WARMUP=$VOICE_AI_WARMUP"
          "AZURE_AI_EMBED_DEPLOYMENT=$AZURE_AI_EMBED_DEPLOYMENT"
          "AZURE_AI_SCOPE=$AZURE_AI_SCOPE"
          "AZURE_SPEECH_REGION=$AZURE_SPEECH_REGION"
          "AZURE_SPEECH_RESOURCE_ID=$AZURE_SPEECH_RESOURCE_ID"
          "TOOLAPI_URL=$TOOLAPI_URL"
          "TOOLAPI_BEARER=secretref:toolapi-bearer"
          "DEFAULT_CUSTOMER_ID=$DEFAULT_CUSTOMER_ID"
          "RM_DISPLAY_NAME=$RM_DISPLAY_NAME"
          "FAST_NUDGE_TIMEOUT_MS=$FAST_NUDGE_TIMEOUT_MS"
          "FAST_PATH_HEADSTART_MS=$FAST_PATH_HEADSTART_MS"
          "NUDGE_FRESHNESS_MS=$NUDGE_FRESHNESS_MS"
          "NUDGE_TEAMS_TIMEOUT_MS=$NUDGE_TEAMS_TIMEOUT_MS"
          "NUDGE_MIN_CONFIDENCE=$NUDGE_MIN_CONFIDENCE" )

# Teams / Power Automate webhook (in-call synopsis + nudges). Optional: if unset the
# call still works and nudges simply won't post to Teams.
if [[ -n "${TEAMS_WEBHOOK_URL:-}" ]]; then
  SECRETS+=( "teams-webhook=$TEAMS_WEBHOOK_URL" )
  ENVVARS+=( "TEAMS_WEBHOOK_URL=secretref:teams-webhook" )
fi
# Optional dedicated minimal workflow for live nudges (falls back to TEAMS_WEBHOOK_URL).
if [[ -n "${TEAMS_NUDGE_WEBHOOK_URL:-}" ]]; then
  SECRETS+=( "teams-nudge-webhook=$TEAMS_NUDGE_WEBHOOK_URL" )
  ENVVARS+=( "TEAMS_NUDGE_WEBHOOK_URL=secretref:teams-nudge-webhook" )
fi
# Step 7 scheduling: optional RM-owned Power Automate flows. If unset, the booking page
# serves synthetic working-hours availability and records bookings only.
if [[ -n "${SCHEDULE_WEBHOOK_URL:-}" ]]; then
  SECRETS+=( "schedule-webhook=$SCHEDULE_WEBHOOK_URL" )
  ENVVARS+=( "SCHEDULE_WEBHOOK_URL=secretref:schedule-webhook" )
fi
if [[ -n "${SCHEDULE_AVAILABILITY_WEBHOOK_URL:-}" ]]; then
  SECRETS+=( "schedule-avail-webhook=$SCHEDULE_AVAILABILITY_WEBHOOK_URL" )
  ENVVARS+=( "SCHEDULE_AVAILABILITY_WEBHOOK_URL=secretref:schedule-avail-webhook" )
fi
# Instant "Call your RM" (mobile portal /bank): RM standing meeting link + demo lead time.
if [[ -n "${RM_MEETING_URL:-}" ]]; then
  ENVVARS+=( "RM_MEETING_URL=$RM_MEETING_URL" )
fi
if [[ -n "${CALL_LEAD_SECONDS:-}" ]]; then
  ENVVARS+=( "CALL_LEAD_SECONDS=$CALL_LEAD_SECONDS" )
fi
# Production path: real Teams meeting on the RM's calendar via Microsoft Graph (app-only).
# The client secret is stored as a Container App secret; the rest are plain env vars.
if [[ -n "${GRAPH_TENANT_ID:-}" && -n "${GRAPH_CLIENT_ID:-}" && -n "${GRAPH_CLIENT_SECRET:-}" && -n "${RM_USER_ID:-}" ]]; then
  SECRETS+=( "graph-client-secret=$GRAPH_CLIENT_SECRET" )
  ENVVARS+=( "GRAPH_TENANT_ID=$GRAPH_TENANT_ID" "GRAPH_CLIENT_ID=$GRAPH_CLIENT_ID" \
             "GRAPH_CLIENT_SECRET=secretref:graph-client-secret" "RM_USER_ID=$RM_USER_ID" \
             "MEETING_TIMEZONE=${MEETING_TIMEZONE:-India Standard Time}" \
             "MEETING_DURATION_MINUTES=${MEETING_DURATION_MINUTES:-30}" )
fi

az extension add --name containerapp --upgrade -y 2>/dev/null || true
if az containerapp show -n "$VA_APP" -g "$AZ_RG" -o none 2>/dev/null; then
  log "Updating existing Container App $VA_APP (additive — preserves Teams webhook) ..."
  az containerapp identity assign -n "$VA_APP" -g "$AZ_RG" --user-assigned "$UAMI_ID" -o none
  az containerapp registry set -n "$VA_APP" -g "$AZ_RG" --server "$ACR_LOGIN_SERVER" --identity "$UAMI_ID" -o none
  az containerapp secret set -n "$VA_APP" -g "$AZ_RG" --secrets "${SECRETS[@]}" -o none
  az containerapp update -n "$VA_APP" -g "$AZ_RG" --image "$IMAGE_REF" \
    --min-replicas 1 --max-replicas 3 --set-env-vars "${ENVVARS[@]}" -o none
else
  log "Creating Container App $VA_APP in $NAME_ACA_ENV ..."
  az containerapp create -n "$VA_APP" -g "$AZ_RG" --environment "$NAME_ACA_ENV" \
    --image "$IMAGE_REF" \
    --user-assigned "$UAMI_ID" \
    --registry-server "$ACR_LOGIN_SERVER" --registry-identity "$UAMI_ID" \
    --target-port 3000 --ingress external --min-replicas 1 --max-replicas 3 \
    --secrets "${SECRETS[@]}" --env-vars "${ENVVARS[@]}" \
    --tags "$PROJECT_TAG" phase=phase9 -o none
fi
# Re-assert the project tag (version-robust) so wipe's tag guard always recognises this app.
VA_ID="$(az containerapp show -n "$VA_APP" -g "$AZ_RG" --query id -o tsv 2>/dev/null || true)"
if [[ -n "$VA_ID" ]]; then
  az resource tag --ids "$VA_ID" --tags "$PROJECT_TAG" phase=phase9 -o none 2>/dev/null || true
  VTAG="$(az resource show --ids "$VA_ID" --query "tags.${PROJECT_TAG_KEY}" -o tsv 2>/dev/null || true)"
  [[ "$VTAG" == "$PROJECT_TAG_VALUE" ]] && ok "Tagged $VA_APP" || warn "Could not confirm project tag on $VA_APP — teardown still removes it by name."
fi
ok "Video Assist deployed"

VA_FQDN="$(az containerapp show -n "$VA_APP" -g "$AZ_RG" --query properties.configuration.ingress.fqdn -o tsv)"
VIDEOASSIST_URL="https://$VA_FQDN"
cat > "$SCRIPT_DIR/outputs.env" <<EOF2
# Generated by infra/phase9-videoassist/up.sh on $(date -u --iso-8601=seconds)
export VIDEOASSIST_URL="$VIDEOASSIST_URL"
export VIDEOASSIST_IMAGE="$IMAGE_REF"
EOF2

log "Smoke-testing /healthz ..."
for i in $(seq 1 15); do curl -fsS "$VIDEOASSIST_URL/healthz" >/dev/null 2>&1 && { ok "Video Assist healthy"; break; }; [[ $i -eq 15 ]] && warn "not confirmed yet"; sleep 5; done

HAS_WEBHOOK="$(az containerapp secret list -n "$VA_APP" -g "$AZ_RG" --query "[?name=='teams-webhook'].name | [0]" -o tsv 2>/dev/null || true)"
cat <<EOF3

$(printf '\033[1;36m================ Phase 9 — Video Assist deployed ================\033[0m')
  Video Assist app:    $VIDEOASSIST_URL
  Customer mobile app: $VIDEOASSIST_URL/bank?customer_id=$DEFAULT_CUSTOMER_ID   (tap "Video call your RM")
  Step 7 launches:     $VIDEOASSIST_URL/?customer_id=$DEFAULT_CUSTOMER_ID
  Grounding (Tool API):$TOOLAPI_URL
  Health:              curl -fsS "$VIDEOASSIST_URL/healthz"   (aiReady, grounding, teamsConfigured)
EOF3
if [[ -z "$HAS_WEBHOOK" ]]; then
  printf '\033[1;33m  Teams webhook NOT set.\033[0m Set TEAMS_WEBHOOK_URL in infra/common/env.sh and re-run to wire live nudges.\n'
fi
printf '\033[1;36m================================================================\033[0m\n'
ok "Phase 9 complete."
