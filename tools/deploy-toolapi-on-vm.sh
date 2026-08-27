#!/usr/bin/env bash
# tools/deploy-toolapi-on-vm.sh
#
# Deploys the FastAPI Tool API onto the phase10 VM as a NATIVE systemd service — no
# container, no ACR. Mirrors the reference repo's infra/deploy/04_deploy_app.sh.
#
# Layout produced on the VM (must match rmx-toolapi.service exactly, see below):
#   /opt/rmx/toolapi/backend/app/...        the FastAPI application package
#   /opt/rmx/toolapi/backend/requirements.txt
#   /opt/rmx/toolapi/data/csv/              <- repo data/csv              (DATA_DIR)
#   /opt/rmx/toolapi/data/knowledge_base/   <- repo data/knowledge_base   (KB_DIR)
#   /opt/rmx/toolapi/data/sop/              <- repo docs/sop              (SOP_DIR)
#   /opt/rmx/toolapi/venv/                  virtualenv with requirements.txt installed
#   /opt/rmx/etc/toolapi.env                root-owned 0600 app config (EnvironmentFile)
#
# ⚠️ THE data/sop MAPPING IS NOT A TYPO. There is no data/sop directory in this repo. The SOP
# corpus lives in docs/sop and the container image renamed it on the way in — backend/Dockerfile
# line 27 is literally `COPY docs/sop /app/data/sop`. backend/app/config.py:27 then reads
# SOP_DIR (default /app/data/sop). Reproducing the RENAME, not the source path, is what keeps
# the Raw Data explorer working. Copying docs/sop to /opt/rmx/toolapi/docs/sop instead would
# start cleanly and then serve an empty SOP list.
#
# Transport is tar-over-ssh rather than rsync (which the approved plan mentioned): it is the
# idiom already used by tools/deploy-console-on-vm.sh and tools/run-generation-on-vm.sh, and
# it needs only tar+ssh on the jump host. rsync would have to be installed on BOTH ends.
#
# Idempotent — safe to run on every build.
#
# Usage:  bash tools/deploy-toolapi-on-vm.sh
set -euo pipefail
PHASE="toolapi-vm"; export PHASE

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
[[ -n "${RMASSIST_HOST:-}" ]] || die "RMASSIST_HOST missing from $PHASE10_OUT."

# ---------- Load phase2 (AI/Search/ACS) outputs ----------
PHASE2_OUT="$REPO_ROOT/infra/phase2-ai/outputs.env"
[[ -f "$PHASE2_OUT" ]] || die "Phase 2 outputs missing: $PHASE2_OUT (run phase2 first)."
# shellcheck disable=SC1090
source "$PHASE2_OUT"
[[ -n "${FOUNDRY_ENDPOINT:-}" && -n "${SEARCH_ENDPOINT:-}" && -n "${ACS_ENDPOINT:-}" ]] \
  || die "Phase 2 outputs incomplete (need FOUNDRY_ENDPOINT, SEARCH_ENDPOINT, ACS_ENDPOINT)."

# Same token the VM's shared EnvironmentFile already carries (phase10 calls this too).
ensure_toolapi_bearer

# ---------- Sanity-check the sources before touching the VM ----------
for d in backend/app data/csv data/knowledge_base docs/sop; do
  [[ -d "$REPO_ROOT/$d" ]] || die "Missing source directory: $d"
done
[[ -f "$REPO_ROOT/backend/requirements.txt" ]] || die "Missing backend/requirements.txt"

SSH_OPTS=(-i "$VM_SSH_KEY" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 -o LogLevel=ERROR)
# ssh -n: stdin from /dev/null so we never consume run_wave's piped confirmations.
vm_ssh()     { ssh -n "${SSH_OPTS[@]}" "${VM_ADMIN_USER}@${VM_HOST_IP}" "$@"; }
# keeps stdin connected, for streaming a tarball into `tar -x` on the VM
vm_pipe_in() { ssh "${SSH_OPTS[@]}" "${VM_ADMIN_USER}@${VM_HOST_IP}" "$@"; }

APP_DIR="${VM_TOOLAPI_DIR:-/opt/rmx/toolapi}"
APP_USER="${VM_APP_USER:-rmxapp}"
APP_ENV="${VM_TOOLAPI_ENV_FILE:-/opt/rmx/etc/toolapi.env}"
TOOLAPI_PUBLIC_URL="https://${RMASSIST_HOST}${TOOLAPI_PATH_PREFIX:-/api}"

log "Deploying the Tool API to $VM_HOST_IP:$APP_DIR ..."

# ---------- 1. Stage the tree (admin-owned while we write, handed to the service user after) ----------
vm_ssh "sudo mkdir -p $APP_DIR/backend $APP_DIR/data && sudo chown -R ${VM_ADMIN_USER}:${VM_ADMIN_USER} $APP_DIR" \
  || die "Could not prepare $APP_DIR on the VM."
# Remove the old app package so a deleted module cannot linger and keep being imported.
vm_ssh "rm -rf $APP_DIR/backend/app" || true

log "Shipping backend/ ..."
tar -C "$REPO_ROOT/backend" --exclude='__pycache__' --exclude='*.pyc' -czf - app requirements.txt \
  | vm_pipe_in "tar -C $APP_DIR/backend -xzf -" \
  || die "Failed to ship backend/ to the VM."

log "Shipping data/csv, data/knowledge_base and docs/sop ..."
# Each is extracted under the name config.py expects, NOT its repo name (see the note above).
vm_ssh "rm -rf $APP_DIR/data/csv $APP_DIR/data/knowledge_base $APP_DIR/data/sop && mkdir -p $APP_DIR/data"
tar -C "$REPO_ROOT/data" -czf - csv knowledge_base \
  | vm_pipe_in "tar -C $APP_DIR/data -xzf -" \
  || die "Failed to ship the CSV pack / knowledge base."
# docs/sop -> data/sop: reproduce the Dockerfile's rename.
tar -C "$REPO_ROOT/docs" -czf - sop \
  | vm_pipe_in "tar -C $APP_DIR/data -xzf -" \
  || die "Failed to ship the SOP corpus."

# ---------- 2. Application config -> root-owned 0600 EnvironmentFile ----------
# Mirrors the env vars phase4-toolapi/main.bicep injected into the Container App. Written by
# streaming over stdin, never as a command argument (arguments are visible in `ps` and
# /proc/<pid>/cmdline). systemd reads this as root before dropping to the service user, so
# 0600 root:root is both correct and readable by the unit.
log "Writing $APP_ENV (root:root 0600) ..."
APP_ENV_LOCAL="$SCRIPT_DIR/.toolapi.env.rendered"
_shred_app_env() { [[ -f "$APP_ENV_LOCAL" ]] && { shred -u "$APP_ENV_LOCAL" 2>/dev/null || rm -f "$APP_ENV_LOCAL"; }; return 0; }
trap _shred_app_env EXIT
(
  umask 077
  {
    echo "# Contoso RM Assist Tool API config. Generated by tools/deploy-toolapi-on-vm.sh."
    echo "# Root-owned 0600 systemd EnvironmentFile. DO NOT COMMIT."
    echo "TOOLAPI_BEARER_TOKEN=${TOOLAPI_BEARER_TOKEN}"
    echo "FOUNDRY_ENDPOINT=${FOUNDRY_ENDPOINT}"
    echo "FOUNDRY_AOAI_ENDPOINT=${FOUNDRY_AOAI_ENDPOINT:-}"
    echo "FOUNDRY_CHAT_DEPLOYMENT=${FOUNDRY_CHAT_DEPLOYMENT:-}"
    echo "FOUNDRY_EMBED_DEPLOYMENT=${FOUNDRY_EMBED_DEPLOYMENT:-}"
    echo "FOUNDRY_VOICELIVE_MODEL=${VOICELIVE_MODEL:-}"
    echo "FOUNDRY_VOICELIVE_WS_ENDPOINT=${VOICELIVE_WS_ENDPOINT:-}"
    echo "SEARCH_ENDPOINT=${SEARCH_ENDPOINT}"
    echo "SEARCH_INDEX_NAME=${SEARCH_INDEX_NAME:-contoso-retail-policy-index}"
    echo "ACS_ENDPOINT=${ACS_ENDPOINT}"
    echo "ACS_CALLER_NUMBER=${ACS_CALLER_NUMBER:-}"
    echo "ACS_DEFAULT_RM_PHONE=${ACS_DEFAULT_RM_PHONE:-}"
    echo "ACS_DEFAULT_CUSTOMER_PHONE=${ACS_DEFAULT_CUSTOMER_PHONE:-}"
    echo "ACS_PUBLIC_BASE_URL=${ACS_PUBLIC_BASE_URL:-$TOOLAPI_PUBLIC_URL}"
    echo "ACS_TRANSCRIPTION_LOCALE=${ACS_TRANSCRIPTION_LOCALE:-en-US}"
    echo "ACS_ENABLE_INTERMEDIATE_TRANSCRIPTS=${ACS_ENABLE_INTERMEDIATE_TRANSCRIPTS:-false}"
    echo "ACS_COGNITIVE_SERVICES_ENDPOINT=${ACS_COGNITIVE_SERVICES_ENDPOINT:-}"
    echo "ACS_ENABLE_MEDIA_SPEECH_FALLBACK=${ACS_ENABLE_MEDIA_SPEECH_FALLBACK:-true}"
    echo "ACS_TRANSCRIPTION_MODE=${ACS_TRANSCRIPTION_MODE:-media}"
    echo "AI_REASONING_DEPLOYMENTS=${AI_REASONING_DEPLOYMENTS:-}"
    echo "VOICE_AI_REASONING_EFFORT=${VOICE_AI_REASONING_EFFORT:-low}"
    # Everything is one origin now, so this can be exact rather than "*" as the Container App
    # used. Widen via CORS_ORIGINS if a second origin is ever needed.
    echo "CORS_ORIGINS=${CORS_ORIGINS:-https://${RMASSIST_HOST}}"
    true
  } > "$APP_ENV_LOCAL"
)
vm_ssh "sudo install -o root -g root -m 0600 /dev/null $APP_ENV" || die "Could not create $APP_ENV."
vm_pipe_in "sudo tee $APP_ENV >/dev/null" < "$APP_ENV_LOCAL" || die "Could not write $APP_ENV."
vm_ssh "sudo chmod 0600 $APP_ENV && sudo chown root:root $APP_ENV"
_shred_app_env

# ---------- 3. Virtualenv ----------
# Created once and reused; pip install is re-run every deploy so a requirements change is
# picked up. --upgrade keeps it converged without needing to detect "did requirements change".
log "Creating/refreshing the virtualenv and installing requirements (this can take a few minutes) ..."
vm_ssh "set -e
  test -d $APP_DIR/venv || python3 -m venv $APP_DIR/venv
  $APP_DIR/venv/bin/python -m pip install --upgrade pip setuptools wheel >/dev/null
  $APP_DIR/venv/bin/python -m pip install --upgrade -r $APP_DIR/backend/requirements.txt
" || die "Dependency install failed on the VM. Re-run with: ssh ${VM_ADMIN_USER}@${VM_HOST_IP} '$APP_DIR/venv/bin/python -m pip install -r $APP_DIR/backend/requirements.txt'"

# ---------- 4. Hand the tree to the unprivileged service user and start ----------
vm_ssh "sudo chown -R ${APP_USER}:${APP_USER} $APP_DIR" || die "Could not chown $APP_DIR to $APP_USER."
log "Starting rmx-toolapi.service ..."
vm_ssh "sudo systemctl daemon-reload && sudo systemctl enable rmx-toolapi.service >/dev/null 2>&1; sudo systemctl restart rmx-toolapi.service" \
  || die "Failed to start rmx-toolapi.service. Logs: ssh ${VM_ADMIN_USER}@${VM_HOST_IP} sudo journalctl -u rmx-toolapi -n 60"

# ---------- 5. Local health check (loopback, on the VM) ----------
# Checked on the VM FIRST so a failure here is unambiguously the app, not Caddy.
log "Health check on the VM loopback ..."
LOCAL_OK=0
for i in $(seq 1 30); do
  if vm_ssh "curl -fsS --max-time 5 http://${TOOLAPI_HOST:-127.0.0.1}:${TOOLAPI_PORT:-8000}/healthz >/dev/null 2>&1"; then
    LOCAL_OK=1; break
  fi
  sleep 4
done
if [[ "$LOCAL_OK" != "1" ]]; then
  warn "The Tool API is not answering on ${TOOLAPI_HOST:-127.0.0.1}:${TOOLAPI_PORT:-8000}. Recent logs:"
  vm_ssh 'sudo journalctl -u rmx-toolapi --no-pager -n 60' 2>/dev/null || true
  die "rmx-toolapi.service did not become healthy on the VM. This is the APP, not Caddy —
check the unit's uvicorn ExecStart, the venv, and that DATA_DIR/KB_DIR/SOP_DIR exist under $APP_DIR/data."
fi
ok "Tool API healthy on the VM loopback."

# ---------- 6. Health-GATE through Caddy (the real proof) ----------
# This is the FIRST end-to-end proof that Caddy's /api route and its strip_prefix work: the
# request goes in as /api/healthz over TLS and must reach the app as /healthz.
log "Health gate: GET ${TOOLAPI_PUBLIC_URL}/healthz (through Caddy) ..."
GATE_OK=0
for i in $(seq 1 20); do
  if curl -fsS --max-time 10 --resolve "${RMASSIST_HOST}:443:${VM_HOST_IP}" \
       "${TOOLAPI_PUBLIC_URL}/healthz" >/dev/null 2>&1; then
    GATE_OK=1; break
  fi
  sleep 5
done
if [[ "$GATE_OK" != "1" ]]; then
  warn "Caddy did not serve ${TOOLAPI_PUBLIC_URL}/healthz, but the app IS healthy on the VM loopback."
  warn "That combination narrows it down to the reverse proxy, not the application."
  vm_ssh 'sudo journalctl -u caddy --no-pager -n 40' 2>/dev/null || true
  die "Health gate FAILED. IF THIS FAILS, CHECK THE CADDY /api ROUTE IN /etc/caddy/Caddyfile.
The app answered on 127.0.0.1:${TOOLAPI_PORT:-8000}/healthz but /api/healthz did not come back
through Caddy, so the '/api' handle or its 'uri strip_prefix /api' is the prime suspect. Verify:
  ssh ${VM_ADMIN_USER}@${VM_HOST_IP} sudo caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile
  ssh ${VM_ADMIN_USER}@${VM_HOST_IP} 'curl -sv http://127.0.0.1:${TOOLAPI_PORT:-8000}/healthz'
  curl -sv --resolve ${RMASSIST_HOST}:443:${VM_HOST_IP} ${TOOLAPI_PUBLIC_URL}/healthz"
fi
ok "Health gate PASSED — ${TOOLAPI_PUBLIC_URL}/healthz returns 200 through Caddy."
ok "Caddy's /api strip_prefix is proven end to end (request went in as /api/healthz, app saw /healthz)."

# ---------- 7. Outputs ----------
cat > "$SCRIPT_DIR/../infra/phase10-vmhost/toolapi-outputs.env" <<EOF
# Generated by tools/deploy-toolapi-on-vm.sh on $(date -u --iso-8601=seconds)
export TOOLAPI_URL="$TOOLAPI_PUBLIC_URL"
export TOOLAPI_BEARER_TOKEN="$TOOLAPI_BEARER_TOKEN"
EOF

ok "Tool API deployed. Public base: $TOOLAPI_PUBLIC_URL"
