#!/usr/bin/env bash
# infra/phase10-vmhost/down.sh
#
# Phase 10 — teardown of the data-gen/Caddy VM and its network (billable RG only).
# The full wipe (az group delete) removes all of this with the RG; this script exists for the
# optional per-phase teardown (WIPE_DELETE_RG=0). It NEVER deletes the persistent static IP
# (that lives in $AZ_RG_PERSISTENT) — deleting the VM simply releases the IP association, so
# the next build can re-borrow it.
set -uo pipefail
PHASE="phase10"; export PHASE

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../common/env.sh
source "$SCRIPT_DIR/../common/env.sh"

log "Phase 10 — VM host teardown (persistent static IP is preserved)"
ensure_az_login

# Delete in dependency order: VM (+ its OS disk) -> NIC -> NSG/VNet. Best-effort throughout.
VM_ID="$(az vm show -g "$AZ_RG" -n "$NAME_VM" --query id -o tsv 2>/dev/null || true)"
if [[ -n "$VM_ID" ]]; then
  assert_project_tag "$VM_ID" 2>/dev/null || { [[ "${WIPE_FORCE:-0}" == "1" ]] || warn "VM $NAME_VM lacks project tag; continuing (best-effort)."; }
  OS_DISK_ID="$(az vm show -g "$AZ_RG" -n "$NAME_VM" --query storageProfile.osDisk.managedDisk.id -o tsv 2>/dev/null || true)"
  log "Deleting VM $NAME_VM ..."
  az vm delete -g "$AZ_RG" -n "$NAME_VM" --yes --only-show-errors && ok "Deleted VM $NAME_VM" || warn "VM delete failed."
  if [[ -n "$OS_DISK_ID" ]]; then
    az disk delete --ids "$OS_DISK_ID" --yes --only-show-errors 2>/dev/null && ok "Deleted OS disk." || warn "OS disk delete failed (may already be gone)."
  fi
else
  warn "VM $NAME_VM not found (skipping)."
fi

for pair in "networkInterfaces:$NAME_VM_NIC" "networkSecurityGroups:$NAME_VM_NSG" "virtualNetworks:$NAME_VM_VNET"; do
  restype="${pair%%:*}"; resname="${pair##*:}"
  rid="$(az resource show -g "$AZ_RG" --resource-type "Microsoft.Network/$restype" -n "$resname" --query id -o tsv 2>/dev/null || true)"
  [[ -n "$rid" ]] || { warn "Not found (skipping): $resname"; continue; }
  log "Deleting $restype/$resname ..."
  az resource delete --ids "$rid" --only-show-errors 2>/dev/null && ok "Deleted $resname" || warn "Delete failed: $resname"
done

rm -f "$SCRIPT_DIR/outputs.env" 2>/dev/null || true
ok "Phase 10 teardown complete (persistent static IP $NAME_PERSIST_PIP kept in $AZ_RG_PERSISTENT)."
