#!/usr/bin/env bash
# build_rg.sh — ONE-TIME, NON-BILLABLE foundation for the Contoso Retail
# "RM Assist — Rakesh Sharma" demo.
#
# This is step 1 of the 3-script model:
#   1. build_rg.sh   run ONCE  — creates the resource group + Phase-1 platform
#                    (Log Analytics, ACR, UAMI, Container Apps environment).
#                    Non-billable except ACR Basic (~$5/mo). This is where any
#                    RG-level, one-time setup is applied.
#   2. build.sh      run PER DEMO — the billable stack (AI Foundry, chat + embedding
#                    deployments, AI Search, ACS + Email, Speech, container apps).
#   3. wipe.sh       run AFTER A DEMO — deletes the billable stack but KEEPS this
#                    foundation (and the resource group), so you never re-run build_rg.
#
# All configuration lives in infra/common/env.sh — nothing is required from your shell
# profile. Override any value inline, e.g.:  AZ_REGION=eastus2 bash build_rg.sh
set -eo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# Make every phase/helper script executable (git may not preserve the bit).
find infra videoassist -type f -name '*.sh' -exec chmod +x {} + 2>/dev/null || true

# Run only the foundation portion of the pipeline.
export BUILD_STAGE="foundation"

# shellcheck disable=SC1091
source infra/common/env.sh

if ! az account show >/dev/null 2>&1; then
  az login --use-device-code
fi

echo "Active Azure context:"
az account show --query '{Subscription:name,SubscriptionId:id,TenantId:tenantId}' --output table

echo
echo "Foundation target: resource group $AZ_RG ($AZ_REGION)   [created ONCE — kept across demos]"
echo "Creates (non-billable; ACR Basic ~\$5/mo is the only standing cost):"
echo "  Log Analytics · ACR · UAMI · Container Apps environment"

LOG_FILE="$HOME/rakesh-rm-assist-buildrg-$(date +%Y%m%d-%H%M%S).log"
echo
echo "Starting foundation build. Log: $LOG_FILE"

set +e
bash infra/rebuild-parallel.sh 2>&1 | tee "$LOG_FILE"
STATUS=${PIPESTATUS[0]}
set -e

if [[ "$STATUS" -ne 0 ]]; then
  echo "FOUNDATION BUILD FAILED (exit $STATUS). Review: $LOG_FILE" >&2
  exit "$STATUS"
fi

echo
echo "FOUNDATION READY (resource group + platform created)."
echo "Next (billable):  bash build.sh              # PTU gpt-4.1-mini"
echo "             or:  bash build.sh --type=payg  # pay-as-you-go gpt-4.1-mini"
echo "Teardown after a demo (KEEPS this foundation + the RG): bash wipe.sh"
echo "Log: $LOG_FILE"
