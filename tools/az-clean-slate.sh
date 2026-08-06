#!/usr/bin/env bash
# tools/az-clean-slate.sh
#
# Robustly return this demo's BILLABLE Azure footprint to a verified clean slate — the belt-
# and-suspenders companion to wipe.sh. It:
#   1. waits for any stuck (non-terminal) Cognitive Services accounts to finish transitioning,
#   2. deletes the BILLABLE resource group if it still exists (and waits),
#   3. purges the soft-deleted Foundry + Speech accounts WITH RETRIES (soft-delete lags the RG
#      delete, which is why a single purge often "passes" without actually freeing the name),
#   4. VERIFIES the billable RG is gone and the globally-unique names are absent from the
#      soft-deleted list, exiting non-zero if anything survived.
#
# The billable RG contains the VM/disk/NIC/NSG/VNet too, so deleting it removes the VM host as
# well. The PERSISTENT resource group ($AZ_RG_PERSISTENT — the static IP + committed cert) is
# NEVER touched: this script only ever targets $AZ_RG, and refuses to run if the two are equal.
#
# Names/regions come from infra/common/env.sh (deterministic SUFFIX), so it always targets the
# same resources build.sh would create. Safe to re-run.
#
# Usage:  bash tools/az-clean-slate.sh
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=../infra/common/env.sh
source "$REPO_ROOT/infra/common/env.sh"

# Hard safety: never let a misconfiguration point this purge at the persistent RG.
if [[ "$AZ_RG" == "$AZ_RG_PERSISTENT" ]]; then
  die "Refusing to run: billable RG ($AZ_RG) equals the persistent RG ($AZ_RG_PERSISTENT). The persistent RG must never be purged."
fi

ensure_az_login

RETRY_SLEEP="${CLEAN_RETRY_SLEEP:-20}"
MAX_WAIT_MIN="${CLEAN_MAX_WAIT_MIN:-90}"
deadline=$(( $(date +%s) + MAX_WAIT_MIN * 60 ))

# Wait ONLY while the account is in a NON-terminal provisioning state (Creating/Deleting/...).
# A terminal state (Succeeded/Failed/Canceled) returns immediately so the RG delete removes it.
wait_account_terminal() {  # $1=name $2=rg
  local name="$1" rg="$2" st
  while true; do
    st="$(az cognitiveservices account show -n "$name" -g "$rg" --query properties.provisioningState -o tsv 2>/dev/null)" \
      || { ok "$name not present."; return 0; }
    case "$st" in
      Succeeded|Failed|Canceled|Canceling)
        log "$name is terminal (state=$st) — the RG delete will remove it."; return 0 ;;
    esac
    log "  $name in non-terminal state=$st — waiting for it to settle..."
    (( $(date +%s) < deadline )) || { warn "Timed out waiting for '$name' to reach a terminal state."; return 1; }
    sleep "$RETRY_SLEEP"
  done
}

purge_retry() {  # $1=name $2=region
  local name="$1" region="$2"
  [[ -n "$name" ]] || return 0
  local i
  for (( i=1; i<=${CLEAN_PURGE_ATTEMPTS:-8}; i++ )); do
    if ! az cognitiveservices account list-deleted --query "[?name=='$name'] | [0].name" -o tsv 2>/dev/null | grep -q .; then
      ok "  '$name' not in soft-deleted list (already free)."; return 0
    fi
    if az cognitiveservices account purge -n "$name" -g "$AZ_RG" -l "$region" -o none 2>/dev/null; then
      ok "  Purged soft-deleted '$name' ($region)."; return 0
    fi
    log "  purge attempt $i for '$name' not ready (soft-delete lagging); waiting..."
    sleep "$RETRY_SLEEP"
  done
  warn "  Could not purge '$name' after ${CLEAN_PURGE_ATTEMPTS:-8} attempts."
  return 1
}

log "Clean-slate for BILLABLE RG '$AZ_RG' (Foundry='$NAME_AISERVICES' @ $AZ_REGION_AOAI, Speech='$NAME_SPEECH' @ $AZ_REGION_SPEECH)"
log "Persistent RG '$AZ_RG_PERSISTENT' (static IP + cert) is preserved — never targeted here."

# 1) if an account is mid-transition, wait for it to settle; a healthy account returns at once.
wait_account_terminal "$NAME_AISERVICES" "$AZ_RG" || true
wait_account_terminal "$NAME_SPEECH" "$AZ_RG" || true

# 2) delete the billable RG if it still exists (removes VM/disk/NIC/NSG/VNet + all Azure resources)
if az group show -n "$AZ_RG" -o none 2>/dev/null; then
  log "Deleting resource group '$AZ_RG' (waiting)..."
  az group delete -n "$AZ_RG" --yes -o none && ok "RG deleted." || warn "RG delete reported an error (a stuck resource may remain)."
else
  ok "Resource group '$AZ_RG' already gone."
fi

# 3) purge soft-deleted CogSvc names (with retries) so they free up immediately
purge_retry "$NAME_AISERVICES" "$AZ_REGION_AOAI" || true
purge_retry "$NAME_SPEECH" "$AZ_REGION_SPEECH" || true

# 4) verify — exit non-zero on ANY residue
residue=0
az group show -n "$AZ_RG" -o none 2>/dev/null && { warn "RG '$AZ_RG' still present."; residue=1; }
for acct in "$NAME_AISERVICES" "$NAME_SPEECH"; do
  [[ -n "$acct" ]] || continue
  az cognitiveservices account list-deleted --query "[?name=='$acct'] | [0].name" -o tsv 2>/dev/null | grep -q . \
    && { warn "Soft-deleted name still present: $acct"; residue=1; }
done

echo
if [[ "$residue" == "1" ]]; then
  warn "NOT clean yet. If an account is still deleting, wait and re-run this script."
  exit 1
fi
ok "Clean slate confirmed (billable RG purged; persistent RG + cert preserved)."
ok "Rebuild:  bash build.sh  (foundation auto-created; persistent layer + cert reused)"
