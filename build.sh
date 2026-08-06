#!/usr/bin/env bash
# build.sh — the BILLABLE build for the Contoso Bank "RM Assist" demo.
#
# Step 3 of the 4-script model (run build_persistent.sh ONCE, then build_rg.sh ONCE first):
#   1. build_persistent.sh  run ONCE, EVER — persistent RG + static IP (anchors the reusable cert).
#   2. build_rg.sh   creates the resource group + Phase-1 platform (non-billable).
#   3. build.sh      THIS SCRIPT — provisions the billable stack inside that RG:
#                    AI Foundry account + project, the gpt-5.4 GlobalStandard chat deployment,
#                    the embedding deployment, AI Search, ACS + Email, Speech, the data-gen +
#                    Caddy/TLS VM, the AI dataset + SOP generation (run keylessly ON the VM),
#                    the Tool API, the RAG index, the CRM dashboard, and the Video Assist app.
#                    Generated data/SOPs and the minted cert are auto-committed + pushed to git.
#   4. wipe.sh       full purge of the billable RG (no soft-delete residue); KEEPS the RG-less
#                    persistent layer + committed cert + committed data so build.sh can re-run.
#
# All configuration lives in infra/common/env.sh — nothing is required from your shell
# profile. Override any value inline, e.g.:  TEAMS_WEBHOOK_URL=... bash build.sh
set -eo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# ---- CLI args -------------------------------------------------------------------------
# --regenerate-data  force a FULL rebuild of the dataset + SOP corpus on the VM (gpt-5.4) even
#                    if the committed BASELINE_FROZEN sentinel says they are already frozen.
#                    Without this flag the committed baseline is reused (generation is skipped).
# The chat model is always gpt-5.4 GlobalStandard (created in the RG, deleted by wipe.sh).
REGENERATE_DATA="${REGENERATE_DATA:-0}"
usage() {
  cat <<'USAGE'
Usage: bash build.sh [--regenerate-data]
  (default)          reuse the committed Contoso Bank dataset + SOP corpus (fast; no regeneration).
  --regenerate-data  force a full regenerate + re-freeze of the dataset + SOPs on the VM (gpt-5.4).
  Prereqs (run once): bash build_persistent.sh   then   bash build_rg.sh
USAGE
}
for arg in "$@"; do
  case "$arg" in
    --regenerate-data) REGENERATE_DATA=1 ;;
    -h|--help)         usage; exit 0 ;;
    *)                 echo "Unknown argument: $arg" >&2; usage >&2; exit 2 ;;
  esac
done
export REGENERATE_DATA

# Run only the billable app portion of the pipeline (phase2..phase9); reuse the
# foundation that build_rg.sh created (RG + platform). rebuild-parallel.sh asserts the
# foundation exists and fails fast with guidance if build_rg.sh was not run.
export BUILD_STAGE="apps"

# Make every phase/helper script executable (git may not preserve the bit).
find infra videoassist -type f -name '*.sh' -exec chmod +x {} + 2>/dev/null || true

# shellcheck disable=SC1091
source infra/common/env.sh

if ! az account show >/dev/null 2>&1; then
  az login --use-device-code
fi

echo "Active Azure context:"
az account show --query '{Subscription:name,SubscriptionId:id,TenantId:tenantId}' --output table

echo
echo "Target resource group: $AZ_RG ($AZ_REGION)   [foundation auto-created if missing]"
echo "Persistent layer: $AZ_RG_PERSISTENT (static IP -> $(rmassist_host 2>/dev/null || echo 'rmassist.<ip>.nip.io')) [auto-created once; never wiped]"
echo "Creating AI Foundry account: $NAME_AISERVICES / $NAME_FOUNDRY_PROJECT"
echo "Chat model: CREATE '$AOAI_CHAT_DEPLOYMENT_NAME' ($AOAI_CHAT_MODEL_NAME, $AOAI_CHAT_SKU_NAME) in $AZ_REGION_AOAI — deleted by wipe.sh"
echo "Embedding model: CREATE '$AOAI_EMBED_DEPLOYMENT_NAME' ($AOAI_EMBED_SKU_NAME) — deleted by wipe.sh"
echo "Data-gen + Caddy/TLS VM: CREATE '$NAME_VM' (keyless gpt-5.4 generation via managed identity)"
if [[ "$REGENERATE_DATA" == "1" ]]; then
  echo "Dataset + SOPs: --regenerate-data -> FULL rebuild on the VM (gpt-5.4), then auto-commit + push"
else
  echo "Dataset + SOPs: reuse committed baseline if frozen (BASELINE_FROZEN); else first-time generate on the VM"
fi
if [[ -n "${TEAMS_WEBHOOK_URL:-}" ]]; then
  echo "Teams nudge webhook: configured (length=${#TEAMS_WEBHOOK_URL})"
else
  echo "Teams nudge webhook: not set (video call still works; nudges won't post to Teams)"
fi

LOG_FILE="$HOME/rakesh-rm-assist-build-$(date +%Y%m%d-%H%M%S).log"
echo
echo "Starting app build (phases 2-9). Log: $LOG_FILE"

set +e
bash infra/rebuild-parallel.sh 2>&1 | tee "$LOG_FILE"
STATUS=${PIPESTATUS[0]}
set -e

if [[ "$STATUS" -ne 0 ]]; then
  echo "BUILD FAILED (exit $STATUS). Review: $LOG_FILE" >&2
  exit "$STATUS"
fi

# ---- Auto-commit + push the generated artifacts (data + SOPs + cert) --------------------
# This runs LOCALLY (build.sh's machine is GitHub-authenticated), keeping the repo the single
# source of truth. Idempotent no-op if nothing changed. Disable with COMMIT_ARTIFACTS=0.
if [[ "${COMMIT_ARTIFACTS:-1}" == "1" ]]; then
  echo
  echo "Committing + pushing generated artifacts (data/contosobank, docs/sop, infra/cert)..."
  bash tools/commit-artifacts.sh || echo "WARN: artifact commit/push reported an issue (non-fatal)." >&2
fi

VA_FQDN="$(az containerapp show --name "$NAME_CA_VIDEOASSIST" --resource-group "$AZ_RG" \
  --query properties.configuration.ingress.fqdn --output tsv 2>/dev/null || true)"
DASH_FQDN="$(az containerapp show --name "$NAME_CA_CRM" --resource-group "$AZ_RG" \
  --query properties.configuration.ingress.fqdn --output tsv 2>/dev/null || true)"

echo
[[ -n "$DASH_FQDN" ]] && echo "CRM Dashboard:  https://${DASH_FQDN}"
[[ -n "$VA_FQDN" ]] && {
  echo "Video Assist:   https://${VA_FQDN}"
  echo "Step 7 launch:  https://${VA_FQDN}/?customer_id=${DEFAULT_CUSTOMER_ID}"
  curl -fsS "https://${VA_FQDN}/healthz" | python -m json.tool 2>/dev/null || true
}

echo
echo "Deployed applications:"
az containerapp list --resource-group "$AZ_RG" \
  --query '[].{Application:name,URL:properties.configuration.ingress.fqdn,Revision:properties.latestRevisionName}' \
  --output table

echo
echo "BUILD COMPLETED SUCCESSFULLY"
echo "Log: $LOG_FILE"
echo "Stable RM Assist host (persistent cert): $(rmassist_host 2>/dev/null || echo 'rmassist.<ip>.nip.io')"
echo "Regenerate the dataset + SOPs next time: bash build.sh --regenerate-data"
echo "Tear down the billable stack (KEEPS the RG + foundation): bash wipe.sh"
echo "Full purge incl. the resource group (persistent layer preserved): bash wipe.sh --delete-rg"
