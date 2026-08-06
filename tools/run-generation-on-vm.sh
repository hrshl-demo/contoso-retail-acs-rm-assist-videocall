#!/usr/bin/env bash
# tools/run-generation-on-vm.sh
#
# Runs the dataset + SOP generation ON THE VM (keyless gpt-5.4 via the VM's managed identity)
# and retrieves the artifacts back into this repo so the LOCAL orchestrator — which is the one
# that is GitHub-authenticated — can commit + push them. No key and no GitHub secret ever land
# on the VM.
#
# Flow:
#   1. Sync the generation inputs (data/contosobank, tools, docs/sop) to /opt/rmx/workspace.
#   2. Run tools/ensure-baseline.sh on the VM with the Foundry endpoint exported, gated by the
#      BASELINE_FROZEN sentinel (a no-op once frozen unless --regenerate-data forces it).
#   3. Pull data/contosobank + docs/sop back into this repo.
#
# Usage:
#   bash tools/run-generation-on-vm.sh                 # respect the freeze sentinel (skip if frozen)
#   bash tools/run-generation-on-vm.sh --regenerate-data   # force a full regenerate + re-freeze
set -euo pipefail
PHASE="datagen"; export PHASE

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=../infra/common/env.sh
source "$REPO_ROOT/infra/common/env.sh"
cd "$REPO_ROOT"

FORCE=""
for arg in "$@"; do
  case "$arg" in
    --regenerate-data|--force) FORCE="--force" ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "Unknown argument: $arg" ;;
  esac
done

# ---------- Load phase10 (VM) + phase2 (Foundry) outputs ----------
PHASE10_OUT="$REPO_ROOT/infra/phase10-vmhost/outputs.env"
PHASE2_OUT="$REPO_ROOT/infra/phase2-ai/outputs.env"
[[ -f "$PHASE10_OUT" ]] || die "VM outputs not found ($PHASE10_OUT). Run infra/phase10-vmhost/up.sh first."
# shellcheck disable=SC1090
source "$PHASE10_OUT"
if [[ -f "$PHASE2_OUT" ]]; then
  # shellcheck disable=SC1090
  source "$PHASE2_OUT"
fi
[[ -n "${VM_HOST_IP:-}" && -n "${VM_SSH_KEY:-}" ]] || die "VM connection details missing from $PHASE10_OUT."
[[ -n "${FOUNDRY_AOAI_ENDPOINT:-}" ]] || die "FOUNDRY_AOAI_ENDPOINT missing — run phase2-ai first (it creates gpt-5.4)."
FOUNDRY_CHAT_DEPLOYMENT="${FOUNDRY_CHAT_DEPLOYMENT:-$AOAI_CHAT_DEPLOYMENT_NAME}"

SSH_OPTS=(-i "$VM_SSH_KEY" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 -o LogLevel=ERROR)
vm_ssh() { ssh "${SSH_OPTS[@]}" "${VM_ADMIN_USER}@${VM_HOST_IP}" "$@"; }

WS="/opt/rmx/workspace"

# ---------- 1) Sync generation inputs to the VM ----------
log "Syncing generation inputs to the VM ($VM_HOST_IP:$WS) ..."
vm_ssh "mkdir -p $WS" || die "Could not create workspace on the VM."
# Only send what generation needs; keep it small. docs/sop carries the curated corpus so the
# SOP generator writes its contosobank_* files alongside without disturbing them.
tar -C "$REPO_ROOT" -czf - data/contosobank tools docs/sop \
  | vm_ssh "tar -C $WS -xzf -" \
  || die "Failed to sync inputs to the VM."
ok "Inputs synced."

# ---------- 2) Run generation on the VM (keyless gpt-5.4 via managed identity) ----------
log "Running generation on the VM (ensure-baseline${FORCE:+ $FORCE}) — gpt-5.4 keyless via MSI ..."
# The Foundry endpoint + deployment are exported into the remote shell; DefaultAzureCredential
# on the VM resolves to the system-assigned managed identity via IMDS (no key, no az login).
REMOTE_ENV="FOUNDRY_AOAI_ENDPOINT='${FOUNDRY_AOAI_ENDPOINT}' FOUNDRY_CHAT_DEPLOYMENT='${FOUNDRY_CHAT_DEPLOYMENT}' PYTHON=python3"
if ! vm_ssh "cd $WS && ${REMOTE_ENV} bash tools/ensure-baseline.sh ${FORCE}"; then
  warn "AI generation on the VM failed. Fetching the last logs is not automated; re-run with --regenerate-data after checking the VM."
  die "Generation step failed on the VM."
fi
ok "Generation complete on the VM."

# ---------- 3) Pull artifacts back into this repo ----------
log "Retrieving generated artifacts (data/contosobank + docs/sop) back into the repo ..."
TMP_TGZ="$(mktemp)"
vm_ssh "cd $WS && tar -czf - data/contosobank docs/sop" > "$TMP_TGZ" \
  || { rm -f "$TMP_TGZ"; die "Failed to retrieve artifacts from the VM."; }
tar -C "$REPO_ROOT" -xzf "$TMP_TGZ" || { rm -f "$TMP_TGZ"; die "Failed to unpack retrieved artifacts."; }
rm -f "$TMP_TGZ"
ok "Artifacts retrieved into the repo."

DATASET="$REPO_ROOT/data/contosobank/contosobank_dataset.json"
SENTINEL="$REPO_ROOT/data/contosobank/BASELINE_FROZEN"
[[ -f "$DATASET" ]]  && ok "Dataset present:  data/contosobank/contosobank_dataset.json" || warn "Dataset JSON missing after retrieval!"
[[ -f "$SENTINEL" ]] && ok "Baseline frozen:  data/contosobank/BASELINE_FROZEN"          || warn "Freeze sentinel missing after retrieval!"
log "Next: tools/commit-artifacts.sh commits + pushes these to GitHub (invoked by build.sh)."
