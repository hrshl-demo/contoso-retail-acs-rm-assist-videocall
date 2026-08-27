#!/usr/bin/env bash
# build.sh — the BILLABLE build for the Contoso Retail "RM Assist — Rakesh Sharma" demo.
#
# Step 2 of the 3-script model (run build_rg.sh ONCE first):
#   1. build_rg.sh   creates the resource group + Phase-1 platform (non-billable).
#   2. build.sh      THIS SCRIPT — provisions the billable stack inside that RG:
#                    AI Foundry account + project, the chat model deployment (PAYG or PTU),
#                    the embedding deployment, AI Search, ACS + Email, Speech, the Tool API,
#                    the RAG index, the CRM dashboard, and the Video Assist live-call app.
#   3. wipe.sh       deletes everything build.sh created but KEEPS the foundation + RG,
#                    so you can re-run build.sh for the next demo without build_rg.sh.
#
# All configuration lives in infra/common/env.sh — nothing is required from your shell
# profile, and no flags or inline env vars are needed: `bash build.sh` alone is a complete
# build. To change any setting (webhooks, region, model, rollback levers), EDIT
# infra/common/env.sh (or infra/common/secrets.env) rather than passing it on the command line.
set -eo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# ---- CLI args -------------------------------------------------------------------------
# --type=payg  (default) create a pay-as-you-go gpt-4.1-mini deployment (GlobalStandard).
# --type=ptu             create a 15-PTU gpt-4.1-mini deployment (GlobalProvisionedManaged).
# In BOTH modes the chat deployment is CREATED in the RG and DELETED by wipe.sh.
DEPLOY_TYPE="${DEPLOY_TYPE:-payg}"
usage() {
  cat <<'USAGE'
Usage: bash build.sh [--type=payg|ptu]
  (no arguments)  DEFAULT: create a pay-as-you-go gpt-4.1-mini chat deployment (GlobalStandard).
  --type=payg   (default) same as passing no arguments.
  --type=ptu    instead create a 15-PTU gpt-4.1-mini chat deployment (GlobalProvisionedManaged).
  Run 'bash build_rg.sh' ONCE first to create the resource group + platform.
USAGE
}
for arg in "$@"; do
  case "$arg" in
    --type=*)  DEPLOY_TYPE="${arg#*=}" ;;
    --type)    echo "Use '--type=payg' or '--type=ptu' (with '=')." >&2; exit 2 ;;
    -h|--help) usage; exit 0 ;;
    *)         echo "Unknown argument: $arg" >&2; usage >&2; exit 2 ;;
  esac
done
case "$DEPLOY_TYPE" in
  ptu|payg) ;;
  *) echo "Invalid --type '$DEPLOY_TYPE' (expected 'payg' or 'ptu')." >&2; exit 2 ;;
esac
export DEPLOY_TYPE

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
echo "Creating AI Foundry account: $NAME_AISERVICES / $NAME_FOUNDRY_PROJECT"
if [[ "$DEPLOY_TYPE" == "payg" ]]; then
  echo "Chat model [payg — DEFAULT]: CREATE '$AOAI_CHAT_DEPLOYMENT_NAME' ($AOAI_CHAT_SKU_NAME, pay-as-you-go) — deleted by wipe.sh"
else
  echo "Chat model [--type=ptu]: CREATE '$AOAI_CHAT_DEPLOYMENT_NAME' ($AOAI_CHAT_SKU_NAME, ${AOAI_CHAT_SKU_CAPACITY} PTU) — deleted by wipe.sh"
fi
if [[ "${VOICE_MODEL_ENABLED:-1}" == "1" ]]; then
  echo "Voice model: CREATE '$AOAI_VOICE_DEPLOYMENT_NAME' ($AOAI_VOICE_MODEL_NAME, $AOAI_VOICE_SKU_NAME, reasoning_effort=$VOICE_AI_REASONING_EFFORT) — deleted by wipe.sh"
else
  echo "Voice model: DISABLED (VOICE_MODEL_ENABLED=0) — the live-call path reuses '$AOAI_CHAT_DEPLOYMENT_NAME'"
fi
echo "Embedding model: CREATE '$AOAI_EMBED_DEPLOYMENT_NAME' ($AOAI_EMBED_SKU_NAME) — deleted by wipe.sh"
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
echo "Tear down the billable stack (KEEPS the RG + foundation): bash wipe.sh"
echo "Delete EVERYTHING incl. the resource group: bash wipe.sh --delete-rg"
