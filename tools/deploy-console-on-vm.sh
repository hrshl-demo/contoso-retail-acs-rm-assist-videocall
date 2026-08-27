#!/usr/bin/env bash
# tools/deploy-console-on-vm.sh
#
# Deploys the static "Core Banking & CRM" console (corebank-console/) plus the generated
# Contoso Bank dataset onto the phase10 VM's Caddy webroot (/opt/rmx/web), so it is served
# over the reusable Let's Encrypt TLS host at  https://<rmassist-host>/ .
#
# The console is a zero-dependency static SPA; it fetches ./data/contosobank_dataset.json.
# This script is idempotent — it can run on every build. It uses the same keyless SSH path
# (phase10 SSH key + persistent IP) as tools/run-generation-on-vm.sh; no secrets touch the VM.
#
# Layout produced on the VM:
#   /opt/rmx/web/index.html
#   /opt/rmx/web/assets/{styles.css,app.js}
#   /opt/rmx/web/data/contosobank_dataset.json
#
# Usage:
#   bash tools/deploy-console-on-vm.sh
set -euo pipefail
PHASE="console"; export PHASE

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=../infra/common/env.sh
source "$REPO_ROOT/infra/common/env.sh"
cd "$REPO_ROOT"

# ---------- Load phase10 (VM) outputs ----------
PHASE10_OUT="$REPO_ROOT/infra/phase10-vmhost/outputs.env"
[[ -f "$PHASE10_OUT" ]] || die "VM outputs not found ($PHASE10_OUT). Run infra/phase10-vmhost/up.sh first."
# shellcheck disable=SC1090
source "$PHASE10_OUT"
[[ -n "${VM_HOST_IP:-}" && -n "${VM_SSH_KEY:-}" && -n "${VM_ADMIN_USER:-}" ]] \
  || die "VM connection details missing from $PHASE10_OUT."

CONSOLE_DIR="$REPO_ROOT/corebank-console"
DATASET="$REPO_ROOT/data/contosobank/contosobank_dataset.json"
[[ -d "$CONSOLE_DIR" ]] || die "Console sources not found ($CONSOLE_DIR)."
[[ -f "$CONSOLE_DIR/index.html" ]] || die "Console index.html not found — nothing to deploy."
[[ -f "$DATASET" ]] || die "Dataset not found ($DATASET). Run generation first (build.sh)."

SSH_OPTS=(-i "$VM_SSH_KEY" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 -o LogLevel=ERROR)
# ssh -n: stdin from /dev/null so we never consume run_wave's piped confirmations.
vm_ssh()      { ssh -n "${SSH_OPTS[@]}" "${VM_ADMIN_USER}@${VM_HOST_IP}" "$@"; }
# vm_pipe_in keeps stdin so we can stream a tarball into `tar -x` on the VM.
vm_pipe_in()  { ssh "${SSH_OPTS[@]}" "${VM_ADMIN_USER}@${VM_HOST_IP}" "$@"; }

WEB="/opt/rmx/web/console"

log "Deploying Core Banking & CRM console to the VM ($VM_HOST_IP:$WEB) ..."

# The console lives in a SUBDIRECTORY, not at the webroot. The webroot itself belongs to the
# RM Assist cockpit (tools/deploy-crm-on-vm.sh), which cannot move: videoassist/teams.js:20
# deep-links it as `${CRM_BASE_URL}/?<query>` and frontend-crm/html/index.html references its
# assets absolutely (/app.js, /ui.css). This console has neither constraint — every reference
# it makes is relative (./assets/styles.css, ./assets/app.js, ./data/contosobank_dataset.json)
# — so serving it from /console/ needs no code change at all.
# Caddy's file_server redirects /console -> /console/ and serves index.html for the directory,
# so no Caddyfile route is required for it either.

# Ensure the webroot exists and is writable by the admin user (cloud-init already creates it,
# but this makes the script safe to run against an older VM image too).
vm_ssh "sudo mkdir -p $WEB/assets $WEB/data && sudo chown -R ${VM_ADMIN_USER}:${VM_ADMIN_USER} $WEB" \
  || die "Could not prepare the webroot on the VM."

# Ship the console sources (index.html + assets/), excluding any local test data dir.
tar -C "$CONSOLE_DIR" --exclude='./data' -czf - index.html assets \
  | vm_pipe_in "tar -C $WEB -xzf -" \
  || die "Failed to sync console sources to the VM."

# Ship the dataset into the webroot's data/ directory under the name the SPA fetches.
tar -C "$(dirname "$DATASET")" -czf - "$(basename "$DATASET")" \
  | vm_pipe_in "tar -C $WEB/data -xzf -" \
  || die "Failed to sync the dataset to the VM."

# World-readable so the caddy service user can serve the files.
vm_ssh "chmod -R a+rX $WEB" || warn "Could not relax webroot permissions (Caddy may still read them)."

# Make Caddy pick up the (unchanged) Caddyfile / new files. reload is a no-op-safe hot reload.
vm_ssh "sudo systemctl reload caddy 2>/dev/null || sudo systemctl restart caddy 2>/dev/null || true"

URL="${RMASSIST_URL:-https://${RMASSIST_HOST:-$VM_HOST_IP}/}"
ok "Console deployed. Serving at: ${URL%/}/console/"
log "Tabs: Enterprise Overview + Retail / Business Banking / Corporate — customer 360 with core-banking + CRM views."
log "The RM Assist cockpit is at ${URL%/}/ (deployed by tools/deploy-crm-on-vm.sh)."
