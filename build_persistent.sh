#!/usr/bin/env bash
# build_persistent.sh — ONE-TIME, run-once bootstrap of the PERSISTENT layer for the
# Contoso Bank "RM Assist" demo.
#
# This is the FIRST script in the 4-script model:
#   1. build_persistent.sh   run ONCE, EVER — creates the persistent RG + a STATIC public IP.
#                            The IP anchors the stable hostname  rmassist.<ip>.nip.io  that the
#                            committed Let's Encrypt certificate is bound to. Kept across every
#                            wipe so the cert stays valid forever. Cost: a few USD/month for the
#                            reserved IP.
#   2. build_rg.sh           run once — the non-billable foundation (RG + platform).
#   3. build.sh              run per demo — the billable stack (gpt-5.4 Foundry, VM + Caddy +
#                            cert, data generation on the VM, container apps). Supports
#                            --regenerate-data to force a full dataset + SOP rebuild.
#   4. wipe.sh               run after a demo — full purge of the billable RG (no soft-delete
#                            residue). NEVER touches the persistent RG or the committed cert.
#
# All configuration lives in infra/common/env.sh. Override inline, e.g.:
#   AZ_REGION=eastus2 bash build_persistent.sh
set -eo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

find infra -type f -name '*.sh' -exec chmod +x {} + 2>/dev/null || true

# shellcheck disable=SC1091
source infra/common/env.sh

if ! az account show >/dev/null 2>&1; then
  az login --use-device-code
fi

echo "Active Azure context:"
az account show --query '{Subscription:name,SubscriptionId:id,TenantId:tenantId}' --output table

echo
echo "Persistent target: resource group $AZ_RG_PERSISTENT ($AZ_REGION_PERSISTENT)   [created ONCE — NEVER wiped]"
echo "Creates: a STATIC Standard public IP ($NAME_PERSIST_PIP) that anchors rmassist.<ip>.nip.io"
echo "         — the domain the reusable Let's Encrypt certificate is bound to."

bash infra/persistent/up.sh

HOST="$(rmassist_host)"
echo
echo "PERSISTENT LAYER READY."
echo "Stable host: ${HOST:-<pending>}"
echo "Next (once):    bash build_rg.sh"
echo "Then per demo:  bash build.sh          # billable: gpt-5.4 + VM/Caddy/cert + data-gen"
echo "            or: bash build.sh --regenerate-data   # force a full dataset + SOP rebuild"
