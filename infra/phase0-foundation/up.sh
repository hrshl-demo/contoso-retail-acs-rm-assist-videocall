#!/usr/bin/env bash
# infra/phase0-foundation/up.sh
#
# Phase 0 — Foundation. Creates the resource group (free); NO BILLABLE RESOURCES.
#   1. Verify az login + subscription
#   2. Create the resource group (self-contained build — this RG holds EVERYTHING)
#   3. Verify required CLI tools and extensions
#   4. Register required resource providers (idempotent)
#   5. Print planned naming and confirm before Phase 1
set -euo pipefail
PHASE="phase0"; export PHASE
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../common/env.sh
source "$SCRIPT_DIR/../common/env.sh"

log "Phase 0 — Foundation"
ensure_az_login
ensure_rg

log "Checking required CLI tools..."
MISSING_NOW=0
for cmd in az jq curl python3 sha256sum; do
  if ! command -v "$cmd" >/dev/null 2>&1; then warn "Missing: $cmd"; MISSING_NOW=1
  else ok "Found: $cmd"; fi
done
[[ $MISSING_NOW -eq 1 ]] && die "Install missing tools above before continuing."

log "Ensuring az CLI extensions..."
for ext in containerapp; do
  if az extension show --name "$ext" >/dev/null 2>&1; then ok "Extension installed: $ext"
  else log "Installing extension: $ext"; az extension add --name "$ext" --only-show-errors; ok "Installed: $ext"; fi
done

log "Registering resource providers (idempotent)..."
PROVIDERS=(
  Microsoft.App                     # Container Apps
  Microsoft.ContainerRegistry       # ACR
  Microsoft.ManagedIdentity         # UAMI
  Microsoft.OperationalInsights     # Log Analytics
  Microsoft.CognitiveServices       # AOAI + Speech (Voice Live backing)
  Microsoft.Search                  # AI Search
  Microsoft.Communication           # ACS (email)
  Microsoft.Network                 # implicit dep for ACA
  Microsoft.Insights                # diagnostics
)
for rp in "${PROVIDERS[@]}"; do
  state="$(az provider show --namespace "$rp" --query registrationState -o tsv 2>/dev/null || echo NotFound)"
  case "$state" in
    Registered)  ok "$rp already Registered" ;;
    Registering) warn "$rp currently Registering" ;;
    *) log "Registering $rp ..."; az provider register --namespace "$rp" --only-show-errors >/dev/null; ok "$rp registration triggered" ;;
  esac
done

log "Waiting up to 90s for pending provider registrations..."
for _ in $(seq 1 18); do
  pending=0
  for rp in "${PROVIDERS[@]}"; do
    state="$(az provider show --namespace "$rp" --query registrationState -o tsv 2>/dev/null || echo Unknown)"
    [[ "$state" != "Registered" ]] && pending=1
  done
  [[ $pending -eq 0 ]] && break
  sleep 5
done

cat <<EOM

$(printf '\033[1;36m======== Contoso Retail RM Assist (Rakesh Sharma) — Planned Resource Names ========\033[0m')
Suffix (deterministic):       $SUFFIX
Resource group:               $AZ_RG ($AZ_REGION)   [CREATED by this build]
Project tag:                  $PROJECT_TAG

Phase 1 (platform):
  Log Analytics:              $NAME_LAW
  Container Registry:         $NAME_ACR
  Managed Identity (UAMI):    $NAME_UAMI
  Container Apps Environment: $NAME_ACA_ENV

Phase 2 (AI services — ALL CREATED here, deleted with the RG on wipe):
  AI Search:                  $NAME_SEARCH  (region: $AZ_REGION_SEARCH)
  Comm Services:              $NAME_ACS
  Speech (in-call STT):       $NAME_SPEECH  (region: $AZ_REGION_SPEECH)
  AIServices (AI Foundry):    $NAME_AISERVICES
  Foundry project:            $NAME_FOUNDRY_PROJECT
  Chat deployment:            $AOAI_CHAT_DEPLOYMENT_NAME  ($AOAI_CHAT_SKU_NAME, capacity $AOAI_CHAT_SKU_CAPACITY)
  Embed deployment:           $AOAI_EMBED_DEPLOYMENT_NAME  ($AOAI_EMBED_SKU_NAME)
  Voice Live model:           $VOICELIVE_MODEL (managed identifier, no deployment)

Phase 4/6/9 (Container Apps):
  Tool API:                   $NAME_CA_TOOLAPI
  CRM Dashboard:              $NAME_CA_CRM
  Video Assist (Step 7):      $NAME_CA_VIDEOASSIST
$(printf '\033[1;36m=====================================================================\033[0m')
EOM

ok "Phase 0 complete. Resource group ensured; no billable resources yet."
log "Next: bash infra/phase1-platform/up.sh"
