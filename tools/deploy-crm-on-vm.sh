#!/usr/bin/env bash
# tools/deploy-crm-on-vm.sh
#
# Deploys the RM Assist CRM cockpit (frontend-crm/html) into the phase10 VM's Caddy webroot,
# so it is served as PLAIN STATIC FILES at https://<rmassist-host>/ . nginx is not involved:
# Caddy's `handle { root * /opt/rmx/web; file_server }` block serves these directly.
#
# This replaces frontend-crm/nginx/10-inject-config.sh, which did the same substitution at
# container startup. The placeholder tokens are IDENTICAL to that script's — __TOOLAPI_URL__,
# __TOOLAPI_BEARER__ and __VIDEOASSIST_URL__ — because they are what is actually written in
# frontend-crm/html/index.html:940-942. Only index.html is patched, exactly as nginx did.
#
# WEBROOT LAYOUT — why the cockpit owns "/" and the console moved to "/console/":
#   /opt/rmx/web/index.html          RM Assist cockpit   (this script)
#   /opt/rmx/web/{app,ui}.js, *.css  its assets
#   /opt/rmx/web/console/            Core Banking console (tools/deploy-console-on-vm.sh)
#
# The cockpit MUST be at the webroot; this is forced by the code, not a preference:
#   1. videoassist/teams.js:20 builds every Teams nudge deep link as
#      `${CRM_BASE_URL}/?<query>` — the ROOT path plus a query string. Putting the cockpit
#      on a subpath would break every deep link, which is the capstone of the demo.
#   2. frontend-crm/html/index.html:875-946 references its assets ABSOLUTELY (/ui.css,
#      /refresh.css, /ui.js, /app.js). Under a subpath those would 404.
# The Core Banking console has neither constraint — its refs are all relative (./assets/...,
# ./data/contosobank_dataset.json) — so it relocates to a subdirectory with zero code changes.
#
# Transport is tar-over-ssh, matching tools/deploy-toolapi-on-vm.sh and deploy-console-on-vm.sh.
# Idempotent — safe to run on every build.
#
# Usage:  bash tools/deploy-crm-on-vm.sh
set -euo pipefail
PHASE="crm-vm"; export PHASE

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
[[ -n "${VM_HOST_IP:-}" && -n "${VM_SSH_KEY:-}" && -n "${VM_ADMIN_USER:-}" && -n "${RMASSIST_HOST:-}" ]] \
  || die "VM connection details missing from $PHASE10_OUT."

# The bearer the cockpit calls the Tool API with — same single source as the Tool API itself,
# so the injected token and the one the API validates can never drift apart.
ensure_toolapi_bearer

SRC="$REPO_ROOT/frontend-crm/html"
[[ -f "$SRC/index.html" ]] || die "Cockpit sources not found ($SRC/index.html)."

# Everything is ONE ORIGIN now, so these are all the same host.
ORIGIN="https://${RMASSIST_HOST}"
INJ_TOOLAPI_URL="${ORIGIN}${TOOLAPI_PATH_PREFIX:-/api}"
INJ_VIDEOASSIST_URL="${ORIGIN}${VIDEOASSIST_PATH_PREFIX:-/video}"

SSH_OPTS=(-i "$VM_SSH_KEY" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 -o LogLevel=ERROR)
vm_ssh()     { ssh -n "${SSH_OPTS[@]}" "${VM_ADMIN_USER}@${VM_HOST_IP}" "$@"; }
vm_pipe_in() { ssh "${SSH_OPTS[@]}" "${VM_ADMIN_USER}@${VM_HOST_IP}" "$@"; }

WEB="${VM_WEB_DIR:-/opt/rmx/web}"

log "Deploying the RM Assist cockpit to $VM_HOST_IP:$WEB ..."

# ---------- 1. Stage locally and inject config into index.html ----------
# Injection happens on a LOCAL COPY, never in the repo working tree: patching
# frontend-crm/html/index.html in place would burn the real bearer token into a git-tracked
# file and destroy the placeholders for the next run.
STAGE="$SCRIPT_DIR/.crm-stage"
_clean_stage() { rm -rf "$STAGE" 2>/dev/null || true; }
trap _clean_stage EXIT
_clean_stage
mkdir -p "$STAGE"
cp "$SRC"/* "$STAGE"/

# Same three tokens, same order, as frontend-crm/nginx/10-inject-config.sh.
# '|' delimiter for the same reason it used one: the values are URLs full of '/'.
sed -i.bak \
  -e "s|__TOOLAPI_URL__|${INJ_TOOLAPI_URL}|g" \
  -e "s|__TOOLAPI_BEARER__|${TOOLAPI_BEARER_TOKEN}|g" \
  -e "s|__VIDEOASSIST_URL__|${INJ_VIDEOASSIST_URL}|g" \
  "$STAGE/index.html"
rm -f "$STAGE/index.html.bak"

# Fail loudly if a placeholder survived: a leftover token means the cockpit boots and then
# calls "https://invalid.local", which surfaces much later as a confusing toast.
if grep -qE '__(TOOLAPI_URL|TOOLAPI_BEARER|VIDEOASSIST_URL)__' "$STAGE/index.html"; then
  grep -nE '__(TOOLAPI_URL|TOOLAPI_BEARER|VIDEOASSIST_URL)__' "$STAGE/index.html" >&2 || true
  die "Config injection FAILED — placeholders remain in index.html (see lines above)."
fi
ok "Injected TOOLAPI_URL=$INJ_TOOLAPI_URL VIDEOASSIST_URL=$INJ_VIDEOASSIST_URL (bearer length ${#TOOLAPI_BEARER_TOKEN})."

# ---------- 2. Ship to the webroot ----------
# Only the cockpit's own files are removed, by name. A blanket `rm -rf $WEB/*` would delete
# the Core Banking console at $WEB/console and the dataset it fetches.
vm_ssh "sudo mkdir -p $WEB && sudo chown -R ${VM_ADMIN_USER}:${VM_ADMIN_USER} $WEB" \
  || die "Could not prepare the webroot on the VM."
vm_ssh "cd $WEB && rm -f index.html app.js ui.js ui.css refresh.css" || true

tar -C "$STAGE" -czf - . | vm_pipe_in "tar -C $WEB -xzf -" \
  || die "Failed to ship the cockpit to the VM."
_clean_stage

# Caddy runs as its own user and must be able to read these.
vm_ssh "chmod -R a+rX $WEB" || warn "Could not relax webroot permissions (Caddy may still read them)."

# ---------- 3. Verify through Caddy ----------
log "Verifying the cockpit at ${ORIGIN}/ ..."
CRM_OK=0
for i in $(seq 1 20); do
  if curl -fsS --max-time 10 --resolve "${RMASSIST_HOST}:443:${VM_HOST_IP}" "${ORIGIN}/" >/dev/null 2>&1; then
    CRM_OK=1; break
  fi
  sleep 4
done
[[ "$CRM_OK" == "1" ]] || die "The cockpit did not answer at ${ORIGIN}/.
Caddy serves this straight off disk, so check the webroot and the catch-all handle:
  ssh ${VM_ADMIN_USER}@${VM_HOST_IP} 'ls -la $WEB'
  ssh ${VM_ADMIN_USER}@${VM_HOST_IP} sudo caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile"

# Confirm the served page is the COCKPIT and not the Core Banking console. Both are static
# index.html files under one webroot, so "something answered 200" is not enough to prove the
# right one is at /.
if curl -fsS --max-time 10 --resolve "${RMASSIST_HOST}:443:${VM_HOST_IP}" "${ORIGIN}/" 2>/dev/null | grep -q '__TOOLAPI_URL__'; then
  die "The page served at / still contains raw placeholders — an un-injected index.html is deployed."
fi
ok "Cockpit deployed and serving at ${ORIGIN}/"
log "Core Banking console (if deployed) is at ${ORIGIN}/console/"
