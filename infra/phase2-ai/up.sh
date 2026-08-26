#!/usr/bin/env bash
# infra/phase2-ai/up.sh
#
# Phase 2 — AI services.
# Discovers and references the EXISTING Foundry (AIServices + project + deployments).
# Creates net-new: AI Search, ACS, Email Comm Services, and the required role assignments.

set -euo pipefail
PHASE="phase2"
export PHASE

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../common/env.sh
source "$SCRIPT_DIR/../common/env.sh"

log "Phase 2 — AI services (CREATE Foundry account + project + Search/ACS/Email, then model deployments)"

ensure_az_login
ensure_rg

# ---------- Pre-flight: load Phase 1 outputs ----------
PHASE1_OUT="$SCRIPT_DIR/../phase1-platform/outputs.env"
if [[ ! -f "$PHASE1_OUT" ]]; then
  die "Phase 1 outputs not found at $PHASE1_OUT. Run phase1-platform/up.sh first."
fi
# shellcheck source=../phase1-platform/outputs.env
source "$PHASE1_OUT"
[[ -n "${UAMI_ID:-}" && -n "${UAMI_PRINCIPAL_ID:-}" ]] || die "Phase 1 outputs incomplete."
ok "Loaded Phase 1 outputs (UAMI: $UAMI_PRINCIPAL_ID)"

# ---------- Cost confirmation (BEFORE any billable resource is created) ----------
if [[ "$DEPLOY_TYPE" == "payg" ]]; then
  CHAT_COST_LINE="  - $AOAI_CHAT_MODEL_NAME chat deployment ($AOAI_CHAT_SKU_NAME, pay-as-you-go)
      CREATED on the NEW Foundry account; billed per token used; deleted with the RG on wipe."
else
  CHAT_COST_LINE="  - ${AOAI_CHAT_SKU_CAPACITY}-PTU $AOAI_CHAT_MODEL_NAME ($AOAI_CHAT_SKU_NAME) chat deployment
      BILLED HOURLY (Provisioned Throughput reserves capacity) — the largest cost here.
      CREATED on the NEW Foundry account; deleted with the RG on wipe."
fi
cat <<EOF

$(printf '\033[1;33m================ Phase 2 — Cost notice ================\033[0m')
This phase CREATES (all deleted on wipe — nothing pre-existing is reused):
  - Azure AI Foundry (AIServices) account + project   $NAME_AISERVICES / $NAME_FOUNDRY_PROJECT
$CHAT_COST_LINE
  - $AOAI_EMBED_MODEL_NAME embedding deployment ($AOAI_EMBED_SKU_NAME)  — powers RAG/SOP search
  - Azure AI Search Basic                    ~ \$75 / month (fixed hourly rate, even idle)
      Region: $AZ_REGION_SEARCH$([ "$AZ_REGION_SEARCH" != "$AZ_REGION" ] && echo "   (separate from $AZ_REGION — Search capacity)")
  - Azure Communication Services             pay-per-use (free until you send mail)
  - Email Communication Services             pay-per-use (\$0.00025 per email roughly)
  - Speech (Cognitive Services S0)           pay-per-use (in-call STT + ACS media fallback)
      Region: $AZ_REGION_SPEECH$([ "$AZ_REGION_SPEECH" != "$AZ_REGION" ] && echo "   (separate from $AZ_REGION — SpeechServices not offered there)")
$(printf '\033[1;33m========================================================\033[0m')

EOF
confirm "Proceed with Phase 2 deployment?"
log "Resolving deployer identity..."
DEPLOYER_TYPE_RAW="$(az account show --query user.type -o tsv)"
case "$DEPLOYER_TYPE_RAW" in
  user) DEPLOYER_PRINCIPAL_TYPE="User" ;;
  servicePrincipal) DEPLOYER_PRINCIPAL_TYPE="ServicePrincipal" ;;
  *) die "Unsupported az account user type: $DEPLOYER_TYPE_RAW" ;;
esac
if [[ "$DEPLOYER_PRINCIPAL_TYPE" == "User" ]]; then
  DEPLOYER_OBJECT_ID="$(az ad signed-in-user show --query id -o tsv)"
else
  SP_APPID="$(az account show --query user.name -o tsv)"
  DEPLOYER_OBJECT_ID="$(az ad sp show --id "$SP_APPID" --query id -o tsv)"
fi
ok "Deployer: type=$DEPLOYER_PRINCIPAL_TYPE objectId=$DEPLOYER_OBJECT_ID"

# ---------- Deploy Bicep with bounded ARM monitoring ----------
# `az deployment group create` normally waits inside the CLI until ARM finishes.
# A stalled resource-provider operation can therefore leave the whole parallel
# rebuild silent forever. Start asynchronously, poll the authoritative ARM state,
# print pending operations, cancel a timed-out attempt, and retry once in
# incremental mode so resources completed by the first attempt are reused.
PHASE2_DEPLOY_TIMEOUT_SECONDS="${PHASE2_DEPLOY_TIMEOUT_SECONDS:-1500}"
PHASE2_DEPLOY_POLL_SECONDS="${PHASE2_DEPLOY_POLL_SECONDS:-15}"
PHASE2_DEPLOY_DIAGNOSTIC_SECONDS="${PHASE2_DEPLOY_DIAGNOSTIC_SECONDS:-60}"
PHASE2_DEPLOY_MAX_ATTEMPTS="${PHASE2_DEPLOY_MAX_ATTEMPTS:-2}"

for numeric_var in PHASE2_DEPLOY_TIMEOUT_SECONDS PHASE2_DEPLOY_POLL_SECONDS \
                   PHASE2_DEPLOY_DIAGNOSTIC_SECONDS PHASE2_DEPLOY_MAX_ATTEMPTS; do
  [[ "${!numeric_var}" =~ ^[1-9][0-9]*$ ]] || die "$numeric_var must be a positive integer."
done

ACTIVE_DEPLOYMENT_NAME=""

_dump_phase2_operations() {
  local deployment_name="$1"
  warn "ARM operations not yet succeeded for $deployment_name:"
  az deployment operation group list \
    --resource-group "$AZ_RG" \
    --name "$deployment_name" \
    --query "[?properties.provisioningState!='Succeeded'].{state:properties.provisioningState,type:properties.targetResource.resourceType,name:properties.targetResource.resourceName,code:properties.statusMessage.error.code,message:properties.statusMessage.error.message}" \
    --output table 2>/dev/null || true
}

_cancel_phase2_deployment() {
  local deployment_name="${1:-$ACTIVE_DEPLOYMENT_NAME}"
  [[ -n "$deployment_name" ]] || return 0
  local state
  state="$(az deployment group show \
    --resource-group "$AZ_RG" \
    --name "$deployment_name" \
    --query properties.provisioningState \
    --output tsv 2>/dev/null || true)"
  if [[ "$state" == "Accepted" || "$state" == "Running" ]]; then
    warn "Cancelling ARM deployment $deployment_name (state=$state)..."
    az deployment group cancel \
      --resource-group "$AZ_RG" \
      --name "$deployment_name" \
      --only-show-errors >/dev/null 2>&1 || true
  fi
}

_wait_for_phase2_terminal_state() {
  local deployment_name="$1"
  local max_wait="${2:-180}"
  local started state elapsed
  started="$(date +%s)"
  while true; do
    state="$(az deployment group show \
      --resource-group "$AZ_RG" \
      --name "$deployment_name" \
      --query properties.provisioningState \
      --output tsv 2>/dev/null || true)"
    case "$state" in
      Succeeded|Failed|Canceled|Cancelled|"") return 0 ;;
    esac
    elapsed=$(( $(date +%s) - started ))
    if (( elapsed >= max_wait )); then
      warn "Deployment $deployment_name is still $state after waiting ${max_wait}s for cancellation."
      return 1
    fi
    sleep 10
  done
}

_cleanup_stale_phase2_deployments() {
  local names name
  names="$(az deployment group list \
    --resource-group "$AZ_RG" \
    --query "[?starts_with(name, 'phase2-ai-') && (properties.provisioningState=='Accepted' || properties.provisioningState=='Running')].name" \
    --output tsv 2>/dev/null || true)"
  [[ -n "$names" ]] || return 0
  warn "Found an unfinished Phase 2 deployment from an earlier run. It will be cancelled before retrying."
  while IFS= read -r name; do
    [[ -n "$name" ]] || continue
    _dump_phase2_operations "$name"
    _cancel_phase2_deployment "$name"
    _wait_for_phase2_terminal_state "$name" 180 || true
  done <<< "$names"
}

_on_phase2_interrupt() {
  warn "Phase 2 interrupted. Attempting to cancel the active ARM deployment."
  _cancel_phase2_deployment
  exit 130
}
trap _on_phase2_interrupt INT TERM

_wait_for_phase2_deployment() {
  local deployment_name="$1"
  local started_at now elapsed state last_diagnostic=0
  started_at="$(date +%s)"

  while true; do
    state="$(az deployment group show \
      --resource-group "$AZ_RG" \
      --name "$deployment_name" \
      --query properties.provisioningState \
      --output tsv 2>/dev/null || true)"

    now="$(date +%s)"
    elapsed=$((now - started_at))

    case "$state" in
      Succeeded)
        ok "ARM deployment $deployment_name succeeded in ${elapsed}s."
        return 0
        ;;
      Failed|Canceled|Cancelled)
        warn "ARM deployment $deployment_name ended with state=$state after ${elapsed}s."
        _dump_phase2_operations "$deployment_name"
        return 1
        ;;
      "")
        # ARM can take a few seconds to expose a just-submitted deployment.
        ;;
      *)
        ;;
    esac

    if (( elapsed >= PHASE2_DEPLOY_TIMEOUT_SECONDS )); then
      warn "ARM deployment $deployment_name exceeded ${PHASE2_DEPLOY_TIMEOUT_SECONDS}s."
      _dump_phase2_operations "$deployment_name"
      _cancel_phase2_deployment "$deployment_name"
      _wait_for_phase2_terminal_state "$deployment_name" 180 || true
      return 124
    fi

    if (( elapsed - last_diagnostic >= PHASE2_DEPLOY_DIAGNOSTIC_SECONDS )); then
      log "ARM deployment $deployment_name: state=${state:-Submitting}, elapsed=${elapsed}s"
      _dump_phase2_operations "$deployment_name"
      last_diagnostic="$elapsed"
    fi

    sleep "$PHASE2_DEPLOY_POLL_SECONDS"
  done
}

_submit_phase2_deployment() {
  local deployment_name="$1"
  az deployment group create \
    --resource-group "$AZ_RG" \
    --name "$deployment_name" \
    --mode Incremental \
    --template-file "$SCRIPT_DIR/main.bicep" \
    --parameters \
        location="$AZ_REGION" \
        searchLocation="$AZ_REGION_SEARCH" \
        speechLocation="$AZ_REGION_SPEECH" \
        suffix="$SUFFIX" \
        deployerObjectId="$DEPLOYER_OBJECT_ID" \
        deployerPrincipalType="$DEPLOYER_PRINCIPAL_TYPE" \
        projectTag="$PROJECT_TAG_VALUE" \
        aiServicesName="$NAME_AISERVICES" \
        foundryProjectName="$NAME_FOUNDRY_PROJECT" \
        chatDeploymentName="$AOAI_CHAT_DEPLOYMENT_NAME" \
        embedDeploymentName="$AOAI_EMBED_DEPLOYMENT_NAME" \
        voiceLiveModel="$VOICELIVE_MODEL" \
        acsDataLocation="$ACS_DATA_LOCATION" \
        uamiPrincipalId="$UAMI_PRINCIPAL_ID" \
        searchName="$NAME_SEARCH" \
        acsName="$NAME_ACS" \
        speechName="$NAME_SPEECH" \
    --no-wait \
    --only-show-errors \
    --output none
}

# ---------- Self-heal: remove a Search service stuck in a terminal 'Failed' state ----------
# Azure AI Search occasionally lands in a terminal 'Failed' provisioningState during
# allocation (a regional capacity blip). Because our Search name is deterministic and
# this phase deploys in Incremental mode, an ARM redeploy REUSES that Failed resource —
# so every in-run retry AND every fresh `build.sh` fails identically until it is deleted
# by hand (previously requiring a full `wipe.sh`). This guard deletes a Failed Search (by
# our deterministic name) so the next attempt provisions it clean. A healthy (Succeeded)
# or still-provisioning Search is left untouched and reused, so a good resource is never
# thrown away.
_heal_failed_search() {
  local state
  state="$(az search service show -n "$NAME_SEARCH" -g "$AZ_RG" \
    --query provisioningState -o tsv 2>/dev/null || true)"
  [[ -n "$state" ]] || return 0                     # absent -> nothing to heal
  case "${state,,}" in
    failed|canceled|cancelled) ;;                   # terminal-bad -> fall through and delete
    *) return 0 ;;                                  # succeeded / provisioning / unknown -> keep + reuse
  esac
  warn "AI Search '$NAME_SEARCH' is in a terminal '$state' state from a previous attempt (capacity blip)."
  warn "Deleting it so this deployment can recreate it cleanly (its Bicep role assignments are re-applied too)..."
  if az search service delete -n "$NAME_SEARCH" -g "$AZ_RG" --yes --only-show-errors 2>/dev/null; then
    ok "Removed the Failed AI Search '$NAME_SEARCH'; this attempt will re-create it."
  else
    warn "Could not auto-delete Failed AI Search '$NAME_SEARCH'. Run: az search service delete -n $NAME_SEARCH -g $AZ_RG --yes  then retry."
  fi
}

_cleanup_stale_phase2_deployments

DEPLOYMENT_BASE="phase2-ai-$(date -u +%Y%m%d-%H%M%S)"
DEPLOYMENT_NAME=""
deployment_succeeded=0

for ((attempt=1; attempt<=PHASE2_DEPLOY_MAX_ATTEMPTS; attempt++)); do
  DEPLOYMENT_NAME="${DEPLOYMENT_BASE}-a${attempt}"
  ACTIVE_DEPLOYMENT_NAME="$DEPLOYMENT_NAME"
  # Clear a Search left 'Failed' by a prior attempt/run so this Incremental deploy recreates it.
  _heal_failed_search
  log "Submitting Bicep deployment attempt ${attempt}/${PHASE2_DEPLOY_MAX_ATTEMPTS}: $DEPLOYMENT_NAME"

  if ! _submit_phase2_deployment "$DEPLOYMENT_NAME"; then
    warn "ARM rejected deployment submission $DEPLOYMENT_NAME."
  elif _wait_for_phase2_deployment "$DEPLOYMENT_NAME"; then
    deployment_succeeded=1
    break
  fi

  if (( attempt < PHASE2_DEPLOY_MAX_ATTEMPTS )); then
    warn "Retrying Phase 2 incrementally after a short cooldown; completed resources will be reused."
    sleep 20
  fi
done

ACTIVE_DEPLOYMENT_NAME=""
trap - INT TERM

if (( deployment_succeeded != 1 )); then
  die "Phase 2 ARM deployment did not complete after ${PHASE2_DEPLOY_MAX_ATTEMPTS} attempt(s). See the pending-operation diagnostics above."
fi

ok "Bicep deployment succeeded."

# ---------- Create model deployments on the freshly-created AIServices account ----------
# The account/project were just created by the Bicep above; model deployments must be
# created AFTER the account exists. Both the chat model (PTU or PAYG per DEPLOY_TYPE)
# and the embedding model are CREATED here and DELETED with the RG on wipe.
# Idempotent: an existing deployment of the same name is reused.
_ensure_deployment() {  # role, deployment_name, model_name, model_format, model_version_override, sku_name, sku_capacity
  local role="$1" dep="$2" model="$3" fmt="$4" ver="$5" sku="$6" cap="$7"
  log "Ensuring $role deployment '$dep' (model $model, $sku, capacity $cap) on $NAME_AISERVICES ..."
  if az cognitiveservices account deployment show \
       --name "$NAME_AISERVICES" -g "$AZ_RG" \
       --deployment-name "$dep" -o none 2>/dev/null; then
    ok "$role deployment already exists: $dep (reusing)"
    return 0
  fi
  if [[ -z "$ver" ]]; then
    log "Discovering latest available version of model '$model' in $AZ_REGION ..."
    ver="$(az cognitiveservices account list-models \
      --name "$NAME_AISERVICES" -g "$AZ_RG" \
      --query "sort_by([?name=='${model}'], &version)[-1].version" -o tsv 2>/dev/null || true)"
  fi
  [[ -n "$ver" ]] || die "Could not resolve a version for model '$model' in $AZ_REGION. Set the version override in infra/common/env.sh, or pick a region that offers $model."
  log "Creating $role deployment $dep (model $model v$ver, $sku capacity $cap) ..."
  az cognitiveservices account deployment create \
    --name "$NAME_AISERVICES" -g "$AZ_RG" \
    --deployment-name "$dep" \
    --model-name "$model" \
    --model-version "$ver" \
    --model-format "$fmt" \
    --sku-name "$sku" \
    --sku-capacity "$cap" \
    -o none \
    || die "Failed to create $role deployment '$dep' ($sku). Check $sku quota in $AZ_REGION (az cognitiveservices usage list -l $AZ_REGION)."
  ok "Created $role deployment: $dep"
}

_ensure_deployment "chat" \
  "$AOAI_CHAT_DEPLOYMENT_NAME" "$AOAI_CHAT_MODEL_NAME" "$AOAI_CHAT_MODEL_FORMAT" \
  "${AOAI_CHAT_MODEL_VERSION:-}" "$AOAI_CHAT_SKU_NAME" "$AOAI_CHAT_SKU_CAPACITY"

_ensure_deployment "embedding" \
  "$AOAI_EMBED_DEPLOYMENT_NAME" "$AOAI_EMBED_MODEL_NAME" "$AOAI_EMBED_MODEL_FORMAT" \
  "${AOAI_EMBED_MODEL_VERSION:-}" "$AOAI_EMBED_SKU_NAME" "$AOAI_EMBED_SKU_CAPACITY"

# ---------- Voice (live-call) reasoning deployment ----------
# A THIRD deployment, dedicated to the in-call nudge/answer path. Kept separate from the
# DEPLOY_TYPE-selected chat deployment so the CRM/backend narration path is unaffected.
# Preflight first: if the model is not offered on this account/region we want to fail
# loudly HERE, not silently at the first live nudge during a demo.
_preflight_model_available() {  # model_name
  local model="$1" avail
  avail="$(az cognitiveservices account list-models \
    --name "$NAME_AISERVICES" -g "$AZ_RG" \
    --query "[?name=='${model}'] | length(@)" -o tsv 2>/dev/null || echo 0)"
  [[ "${avail:-0}" -gt 0 ]]
}

FOUNDRY_VOICE_DEPLOYMENT="$AOAI_CHAT_DEPLOYMENT_NAME"
if [[ "${VOICE_MODEL_ENABLED:-1}" != "1" ]]; then
  warn "VOICE_MODEL_ENABLED=0 — skipping the voice deployment; the live-call path reuses $AOAI_CHAT_DEPLOYMENT_NAME."
elif ! _preflight_model_available "$AOAI_VOICE_MODEL_NAME"; then
  die "Model '$AOAI_VOICE_MODEL_NAME' is not offered on $NAME_AISERVICES in $AZ_REGION.
     Inspect what IS available with:
       az cognitiveservices account list-models --name $NAME_AISERVICES -g $AZ_RG --query \"[].{name:name,version:version}\" -o table
     Then either pick an available model via AOAI_VOICE_MODEL_NAME (and a NEW
     AOAI_VOICE_DEPLOYMENT_NAME) in infra/common/env.sh, or set VOICE_MODEL_ENABLED=0
     to keep the live-call path on $AOAI_CHAT_DEPLOYMENT_NAME."
else
  _ensure_deployment "voice" \
    "$AOAI_VOICE_DEPLOYMENT_NAME" "$AOAI_VOICE_MODEL_NAME" "$AOAI_VOICE_MODEL_FORMAT" \
    "${AOAI_VOICE_MODEL_VERSION:-}" "$AOAI_VOICE_SKU_NAME" "$AOAI_VOICE_SKU_CAPACITY"
  FOUNDRY_VOICE_DEPLOYMENT="$AOAI_VOICE_DEPLOYMENT_NAME"
fi

ok "Voice Live model identifier (managed, no deployment needed): $VOICELIVE_MODEL"

# ---------- Capture outputs ----------
log "Capturing deployment outputs..."
OUTPUTS_JSON="$(az deployment group show \
  --resource-group "$AZ_RG" \
  --name "$DEPLOYMENT_NAME" \
  --query properties.outputs -o json)"

SEARCH_ENDPOINT="$(echo "$OUTPUTS_JSON" | jq -r '.searchEndpoint.value')"
ACS_ENDPOINT="$(echo "$OUTPUTS_JSON"    | jq -r '.acsEndpoint.value')"

OUTFILE="$SCRIPT_DIR/outputs.env"
cat > "$OUTFILE" <<EOF
# Generated by infra/phase2-ai/up.sh on $(date -u --iso-8601=seconds)
export SEARCH_ENDPOINT="$SEARCH_ENDPOINT"
export SEARCH_INDEX_NAME="${SEARCH_INDEX_NAME:-contoso-retail-policy-index}"
export ACS_ENDPOINT="$ACS_ENDPOINT"
export FOUNDRY_ENDPOINT="https://${NAME_AISERVICES}.services.ai.azure.com/"
export FOUNDRY_AOAI_ENDPOINT="https://${NAME_AISERVICES}.services.ai.azure.com/openai/v1"
export FOUNDRY_PROJECT_ENDPOINT="https://${NAME_AISERVICES}.services.ai.azure.com/api/projects/${NAME_FOUNDRY_PROJECT}"
export FOUNDRY_CHAT_DEPLOYMENT="$AOAI_CHAT_DEPLOYMENT_NAME"
export FOUNDRY_EMBED_DEPLOYMENT="$AOAI_EMBED_DEPLOYMENT_NAME"
export FOUNDRY_VOICE_DEPLOYMENT="$FOUNDRY_VOICE_DEPLOYMENT"
export VOICELIVE_MODEL="$VOICELIVE_MODEL"
export VOICELIVE_WS_ENDPOINT="wss://${NAME_AISERVICES}.services.ai.azure.com/voice-live/realtime?api-version=2025-10-01&model=${VOICELIVE_MODEL}"
EOF
ok "Outputs saved to $OUTFILE"

# ---------- Summary ----------
cat <<EOF

$(printf '\033[1;36m================ Phase 2 — Deployed ================\033[0m')
CREATED (all deleted with the RG on wipe):
  AIServices account:           $NAME_AISERVICES
  Foundry project:              $NAME_FOUNDRY_PROJECT
  Chat deployment:              $AOAI_CHAT_DEPLOYMENT_NAME   ($AOAI_CHAT_SKU_NAME, capacity $AOAI_CHAT_SKU_CAPACITY)
  Voice deployment:             $FOUNDRY_VOICE_DEPLOYMENT   ($AOAI_VOICE_MODEL_NAME, $AOAI_VOICE_SKU_NAME, reasoning_effort=$VOICE_AI_REASONING_EFFORT)
  Embed deployment:             $AOAI_EMBED_DEPLOYMENT_NAME   ($AOAI_EMBED_SKU_NAME)
  Voice Live (managed):         $VOICELIVE_MODEL
    via WSS:                    wss://${NAME_AISERVICES}.services.ai.azure.com/voice-live/realtime?api-version=2025-10-01&model=${VOICELIVE_MODEL}
  AI Search endpoint:           $SEARCH_ENDPOINT
  ACS endpoint:                 $ACS_ENDPOINT

Role assignments granted (this phase):
  UAMI       -> AIServices  (Cognitive Services User + OpenAI User)
  UAMI       -> FoundryProj (Azure AI User)
  UAMI       -> Search      (Index Data Contributor + Service Contributor)
  UAMI       -> ACS         (Contributor)
  You        -> Search      (Index Data Contributor + Service Contributor)

Wiring values (Foundry/Search/ACS endpoints + deployment names) written to:
  $SCRIPT_DIR/outputs.env  (passed to phase4/phase6 as literal Container App secrets)
$(printf '\033[1;36m=====================================================\033[0m')

EOF

# ---------- HOTFIX v0.14.4: ACS -> Cognitive Services role for transcription ----------
# The Bicep grants the role to the app's UAMI, but ACS real-time transcription
# requires the ACS resource's OWN managed identity to have Cognitive Services User
# on the AIServices account. This role assignment lives outside the Bicep, so it
# must be re-applied after each rebuild. Idempotent and non-destructive.
log "Applying ACS -> Cognitive Services role grant (transcription fix)..."
if bash "$SCRIPT_DIR/grant-acs-cognitive-role.sh"; then
  ok "ACS transcription role grant applied."
else
  warn "grant-acs-cognitive-role.sh failed — run it manually before placing ACS calls."
fi

# Enable Azure Monitor diagnostic logging on ACS so call-leg dispositions
# (EndReason / ResultCategories) are queryable — needed to diagnose why a PSTN
# leg does/doesn't ring. Idempotent.
log "Enabling ACS diagnostic logging to Log Analytics..."
if bash "$SCRIPT_DIR/enable-acs-monitoring.sh"; then
  ok "ACS monitoring enabled."
else
  warn "enable-acs-monitoring.sh failed (non-fatal) — run it manually to get call diagnostics."
fi

ok "Phase 2 complete."
log "Next: bash infra/phase3-data/up.sh  (still being designed — synthetic JSON seed)"
