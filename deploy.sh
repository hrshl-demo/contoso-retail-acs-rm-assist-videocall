#!/usr/bin/env bash
# deploy.sh — one-command deploy for the Contoso Retail "RM Assist — Rakesh Sharma" demo.
#
# ALL-OR-NOTHING, SELF-CONTAINED build. Creates a brand-new resource group and provisions
# EVERYTHING inside it (AI Foundry account + project, the chat model deployment [PTU or PAYG],
# the embedding deployment, AI Search, ACS + Email, Speech, Tool API, RAG index, CRM dashboard,
# and the Video Assist live-call app). Nothing pre-existing is reused. Region and every
# name/value are configurable in infra/common/env.sh.
#
# This is the ONE-SHOT wrapper (foundation + billable stack together). For locked-down
# subscriptions, prefer the 3-script split so RG-level setup runs once:
#   bash build_rg.sh   (once — RG + platform, non-billable)
#   bash build.sh      (per demo — billable stack)   [--type=ptu|payg]
#   bash wipe.sh       (after a demo — keeps the RG + foundation)
#
# All configuration lives in infra/common/env.sh — nothing is required from your shell
# profile. Override any value inline, e.g.:  TEAMS_WEBHOOK_URL=... bash deploy.sh
set -eo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# ---- CLI args -------------------------------------------------------------------------
# --type=ptu   (default) create a 15-PTU gpt-4.1-mini deployment (GlobalProvisionedManaged).
# --type=payg            create a pay-as-you-go gpt-4.1-mini deployment (GlobalStandard).
# In BOTH modes the chat deployment is CREATED in the new RG and DELETED with it on wipe.
# Parsed BEFORE sourcing env.sh so env.sh can select the deployment profile, and exported
# so every phase (spawned by rebuild-parallel) inherits it.
DEPLOY_TYPE="${DEPLOY_TYPE:-ptu}"
usage() {
  cat <<'USAGE'
Usage: bash deploy.sh [--type=ptu|payg]
  --type=ptu    (default) create a 15-PTU gpt-4.1-mini chat deployment (GlobalProvisionedManaged).
  --type=payg   create a pay-as-you-go gpt-4.1-mini chat deployment (GlobalStandard).
  Both modes create the deployment inside the new RG; wipe deletes the whole RG.
USAGE
}
for arg in "$@"; do
  case "$arg" in
    --type=*)  DEPLOY_TYPE="${arg#*=}" ;;
    --type)    echo "Use '--type=ptu' or '--type=payg' (with '=')." >&2; exit 2 ;;
    -h|--help) usage; exit 0 ;;
    *)         echo "Unknown argument: $arg" >&2; usage >&2; exit 2 ;;
  esac
done
case "$DEPLOY_TYPE" in
  ptu|payg) ;;
  *) echo "Invalid --type '$DEPLOY_TYPE' (expected 'ptu' or 'payg')." >&2; exit 2 ;;
esac
export DEPLOY_TYPE

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
echo "Target resource group: $AZ_RG ($AZ_REGION)   [CREATED by this build — all-or-nothing]"
echo "Creating AI Foundry account: $NAME_AISERVICES / $NAME_FOUNDRY_PROJECT"
if [[ "$DEPLOY_TYPE" == "payg" ]]; then
  echo "Chat model [--type=payg]: CREATE '$AOAI_CHAT_DEPLOYMENT_NAME' ($AOAI_CHAT_SKU_NAME, pay-as-you-go) — deleted with the RG on wipe"
else
  echo "Chat model [--type=ptu]: CREATE '$AOAI_CHAT_DEPLOYMENT_NAME' ($AOAI_CHAT_SKU_NAME, ${AOAI_CHAT_SKU_CAPACITY} PTU) — deleted with the RG on wipe"
fi
echo "Embedding model: CREATE '$AOAI_EMBED_DEPLOYMENT_NAME' ($AOAI_EMBED_SKU_NAME) — deleted with the RG on wipe"
if [[ -n "${TEAMS_WEBHOOK_URL:-}" ]]; then
  echo "Teams nudge webhook: configured (length=${#TEAMS_WEBHOOK_URL})"
else
  echo "Teams nudge webhook: not set (video call still works; nudges won't post to Teams)"
fi

LOG_FILE="$HOME/rakesh-rm-assist-deploy-$(date +%Y%m%d-%H%M%S).log"
echo
echo "Starting full rebuild. Log: $LOG_FILE"

set +e
bash infra/rebuild-parallel.sh 2>&1 | tee "$LOG_FILE"
STATUS=${PIPESTATUS[0]}
set -e

if [[ "$STATUS" -ne 0 ]]; then
  echo "DEPLOYMENT FAILED (exit $STATUS). Review: $LOG_FILE" >&2
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
echo "DEPLOYMENT COMPLETED SUCCESSFULLY"
echo "Log: $LOG_FILE"
echo "Tear down the billable stack (KEEPS the resource group $AZ_RG): bash wipe.sh"
echo "Delete EVERYTHING incl. the resource group: bash wipe.sh --delete-rg"
