#!/usr/bin/env bash
# deploy.sh — one-command deploy for the Contoso Retail "RM Assist — Rakesh Sharma" demo.
#
# ALL-OR-NOTHING, SELF-CONTAINED build. Creates a brand-new resource group and provisions
# EVERYTHING inside it (AI Foundry account + project, the gpt-5.4 chat deployment and the
# gpt-5.4-mini voice deployment,
# the embedding deployment, AI Search, ACS + Email, Speech, Tool API, RAG index, CRM dashboard,
# and the Video Assist live-call app). Nothing pre-existing is reused. Region and every
# name/value are configurable in infra/common/env.sh.
#
# This is the ONE-SHOT wrapper (foundation + billable stack together). For locked-down
# subscriptions, prefer the 3-script split so RG-level setup runs once:
#   bash build_rg.sh   (once — RG + platform, non-billable)
#   bash build.sh      (per demo — billable stack)
#   bash wipe.sh       (after a demo — keeps the RG + foundation)
#
# All configuration lives in infra/common/env.sh — nothing is required from your shell
# profile. Override any value inline, e.g.:  TEAMS_WEBHOOK_URL=... bash deploy.sh
set -eo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# ---- CLI args -------------------------------------------------------------------------
# --type=payg|ptu is accepted for backward compatibility but NO LONGER switches the model
# or SKU: this build always creates a single gpt-5.4 GlobalStandard chat deployment plus the
# gpt-5.4-mini voice deployment. Retarget via AOAI_CHAT_* / AOAI_VOICE_* in infra/common/env.sh.
# In BOTH modes the chat deployment is CREATED in the new RG and DELETED with it on wipe.
# Parsed BEFORE sourcing env.sh so env.sh can select the deployment profile, and exported
# so every phase (spawned by rebuild-parallel) inherits it.
DEPLOY_TYPE="${DEPLOY_TYPE:-payg}"
usage() {
  cat <<'USAGE'
Usage: bash deploy.sh [--type=payg|ptu]
  (no arguments)  DEFAULT and recommended.
  --type=payg|ptu accepted for backward compatibility; it does NOT change the model or SKU.
                  This build always creates gpt-5.4 (GlobalStandard) + the gpt-5.4-mini voice deployment.
  Both modes create the deployment inside the new RG; wipe deletes the whole RG.
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

# Make every phase/helper script executable (git may not preserve the bit).
find infra videoassist -type f -name '*.sh' -exec chmod +x {} + 2>/dev/null || true

# shellcheck disable=SC1091
source infra/common/env.sh

# Azure login + subscription are already configured in the environment (e.g. the jump VM).
# We only pin the target subscription for az commands in THIS process — no 'az login',
# no 'az account show'.
[[ -n "${AZ_SUBSCRIPTION_ID:-}" ]] && az account set --subscription "$AZ_SUBSCRIPTION_ID" >/dev/null 2>&1 || true

echo
echo "Target resource group: $AZ_RG ($AZ_REGION)   [CREATED by this build — all-or-nothing]"
echo "Creating AI Foundry account: $NAME_AISERVICES / $NAME_FOUNDRY_PROJECT"
echo "Chat model: CREATE '$AOAI_CHAT_DEPLOYMENT_NAME' ($AOAI_CHAT_MODEL_NAME, $AOAI_CHAT_SKU_NAME) in $AZ_REGION_AOAI — deleted with the RG on wipe"
if [[ "${VOICE_MODEL_ENABLED:-1}" == "1" ]]; then
  echo "Voice model: CREATE '$AOAI_VOICE_DEPLOYMENT_NAME' ($AOAI_VOICE_MODEL_NAME, $AOAI_VOICE_SKU_NAME, reasoning_effort=$VOICE_AI_REASONING_EFFORT) in $AZ_REGION_AOAI — deleted with the RG on wipe"
else
  echo "Voice model: DISABLED (VOICE_MODEL_ENABLED=0) — the live-call path reuses '$AOAI_CHAT_DEPLOYMENT_NAME'"
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

print_demo_urls

echo
echo "DEPLOYMENT COMPLETED SUCCESSFULLY"
echo "Log: $LOG_FILE"
echo "Tear down EVERYTHING billable (persistent IP + cert preserved): bash wipe.sh"
echo "Keep the RG + platform for a faster next build:                 bash wipe.sh --keep-rg"
