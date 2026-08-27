#!/usr/bin/env bash
# infra/phase10-vmhost/up.sh
#
# Phase 10 — Data-generation + Caddy/TLS VM (billable; deleted on wipe).
#
# Creates the Ubuntu VM, associates the PERSISTENT static IP, and manages the Let's Encrypt
# certificate with a "mint once, reuse forever" policy:
#   • If an ENCRYPTED cert is stored in infra/cert/ for THIS host -> decrypt it to a temp
#     file, PRE-SEED it onto the VM, shred the plaintext, and start Caddy, which serves the
#     existing cert WITHOUT any Let's Encrypt call.
#   • Otherwise (very first build)                     -> start Caddy, let it obtain the cert
#     via HTTP-01, then export it, encrypt it into infra/cert/ via tools/cert_store.sh and
#     write .cert-lock.json so the next build (and everyone who pulls the repo) reuses it.
#     The private key is never written to a git-tracked path in the clear.
# Finally it grants the VM's system-assigned managed identity keyless access to the gpt-5.4
# Foundry account, so generation on the VM needs no key.
set -euo pipefail
PHASE="phase10"; export PHASE

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=../common/env.sh
source "$SCRIPT_DIR/../common/env.sh"

log "Phase 10 — application host (Caddy TLS ingress + Tool API + Video Assist + CRM cockpit)"
ensure_az_login
ensure_rg

# ---------- Prior-phase outputs: the SHARED platform UAMI (phase1) ----------
# The VM carries this identity so the app services inherit the Foundry/Search/ACS role
# assignments phase2 already granted to it. Re-keying those grants to a VM principal would
# be circular: phase2 runs before this VM exists.
#
# outputs.env is git-ignored, so a FRESH CLONE (e.g. on the jump host) will not have it even
# though the Azure resources exist. build.sh already calls assert_foundation_present, which
# regenerates it — but this phase can also be run standalone, so reuse the same env.sh helper
# rather than dying on a file that is trivially reconstructible from Azure.
PHASE1_OUT="$SCRIPT_DIR/../phase1-platform/outputs.env"
[[ -f "$PHASE1_OUT" ]] || regen_phase1_outputs
[[ -f "$PHASE1_OUT" ]] || die "Missing outputs: $PHASE1_OUT (run phase1 first)."
# shellcheck disable=SC1090
source "$PHASE1_OUT"
[[ -n "${UAMI_ID:-}" && -n "${UAMI_CLIENT_ID:-}" ]] \
  || die "Incomplete phase1 outputs (need UAMI_ID and UAMI_CLIENT_ID). Re-run 'bash build_rg.sh'."
ok "Loaded phase1 outputs (UAMI client id ${UAMI_CLIENT_ID})"

# ---------- Require the persistent layer (static IP -> stable host) ----------
PERSIST_PIP_ID="$(az network public-ip show -g "$AZ_RG_PERSISTENT" -n "$NAME_PERSIST_PIP" --query id -o tsv 2>/dev/null || true)"
PERSIST_IP="$(persist_ip)"
if [[ -z "$PERSIST_PIP_ID" || -z "$PERSIST_IP" ]]; then
  die "Persistent static IP not found ($NAME_PERSIST_PIP in $AZ_RG_PERSISTENT). Run 'bash build_persistent.sh' ONCE first."
fi
RMASSIST_HOST="$(rmassist_host)"
[[ -n "$RMASSIST_HOST" ]] || die "Could not derive the rmassist host from the static IP."
export RMASSIST_HOST
ok "Persistent host: $RMASSIST_HOST  (static IP $PERSIST_IP)"

# ---------- Local SSH keypair (orchestrator -> VM; private key is git-ignored) ----------
mkdir -p "$VM_SSH_KEY_DIR"
SSH_KEY="$VM_SSH_KEY_DIR/$VM_SSH_KEY_NAME"
if [[ ! -f "$SSH_KEY" ]]; then
  log "Generating SSH keypair for the VM host ($SSH_KEY) ..."
  ssh-keygen -t ed25519 -N "" -C "rmx-vm-host" -f "$SSH_KEY" -q || die "ssh-keygen failed."
fi
SSH_PUB="$(cat "${SSH_KEY}.pub")"

# ---------- Render cloud-init from the template ----------
if [[ "$LETSENCRYPT_STAGING" == "1" ]]; then
  ACME_CA="https://acme-staging-v02.api.letsencrypt.org/directory"
  warn "LETSENCRYPT_STAGING=1 — minting from the LE STAGING CA (untrusted certs, no rate limits)."
else
  ACME_CA="https://acme-v02.api.letsencrypt.org/directory"
fi
CLOUD_INIT_RENDERED="$SCRIPT_DIR/.cloud-init.rendered.yaml"
sed -e "s#__RMASSIST_HOST__#${RMASSIST_HOST}#g" \
    -e "s#__LETSENCRYPT_EMAIL__#${LETSENCRYPT_EMAIL}#g" \
    -e "s#__ACME_CA__#${ACME_CA}#g" \
    -e "s#__ADMIN_USER__#${VM_ADMIN_USER}#g" \
    -e "s#__APP_USER__#${VM_APP_USER}#g" \
    "$SCRIPT_DIR/cloud-init.yaml" > "$CLOUD_INIT_RENDERED"
CLOUD_INIT_B64="$(base64 -w0 "$CLOUD_INIT_RENDERED" 2>/dev/null || base64 "$CLOUD_INIT_RENDERED" | tr -d '\n')"

# ---------- Render the real Caddyfile + systemd units ----------
# Rendered locally, pushed to the VM after it boots. Caddy does not check upstream liveness
# at startup, so the full config (including the /api and /video reverse_proxy blocks) is
# valid before the app services exist — which is what lets the cert flow run first.
CADDYFILE_RENDERED="$SCRIPT_DIR/.Caddyfile.rendered"
sed -e "s#__RMASSIST_HOST__#${RMASSIST_HOST}#g" \
    -e "s#__LETSENCRYPT_EMAIL__#${LETSENCRYPT_EMAIL}#g" \
    -e "s#__ACME_CA__#${ACME_CA}#g" \
    -e "s#__TOOLAPI_BIND__#${TOOLAPI_BIND}#g" \
    -e "s#__VIDEOASSIST_BIND__#${VIDEOASSIST_BIND}#g" \
    "$SCRIPT_DIR/Caddyfile.tmpl" > "$CADDYFILE_RENDERED"

TOOLAPI_UNIT_RENDERED="$SCRIPT_DIR/.rmx-toolapi.service.rendered"
sed -e "s#__APP_USER__#${VM_APP_USER}#g" \
    -e "s#__TOOLAPI_DIR__#${VM_TOOLAPI_DIR}#g" \
    -e "s#__TOOLAPI_HOST__#${TOOLAPI_HOST}#g" \
    -e "s#__TOOLAPI_PORT__#${TOOLAPI_PORT}#g" \
    -e "s#__ENV_FILE__#${VM_ENV_FILE}#g" \
    -e "s#__APP_ENV_FILE__#${VM_TOOLAPI_ENV_FILE}#g" \
    "$SCRIPT_DIR/rmx-toolapi.service.tmpl" > "$TOOLAPI_UNIT_RENDERED"

VIDEOASSIST_UNIT_RENDERED="$SCRIPT_DIR/.rmx-videoassist.service.rendered"
sed -e "s#__APP_USER__#${VM_APP_USER}#g" \
    -e "s#__VIDEOASSIST_DIR__#${VM_VIDEOASSIST_DIR}#g" \
    -e "s#__VIDEOASSIST_HOST__#${VIDEOASSIST_HOST}#g" \
    -e "s#__VIDEOASSIST_PORT__#${VIDEOASSIST_PORT}#g" \
    -e "s#__NODE_BIN__#${VM_NODE_BIN}#g" \
    -e "s#__ENV_FILE__#${VM_ENV_FILE}#g" \
    -e "s#__APP_ENV_FILE__#${VM_VIDEOASSIST_ENV_FILE}#g" \
    "$SCRIPT_DIR/rmx-videoassist.service.tmpl" > "$VIDEOASSIST_UNIT_RENDERED"
ok "Rendered Caddyfile + 2 systemd units."

# ---------- Preflight: is the requested VM size actually deployable here? ----------
# A capacity restriction must abort BEFORE any billable resource is created. Without this,
# ARM only rejects the size at VM preflight — i.e. mid-wave, after phase2/phase4 have
# already provisioned real resources — which is exactly how Standard_B2s burned a build in
# southindia. `az vm list-skus` reports both availability and per-subscription restrictions.
_preflight_vm_size() {
  local size="$1" region="$2" found restrictions detail
  log "Preflight: checking VM size '$size' is available in $region ..."
  local hint="     List sizes that ARE available and unrestricted:
       az vm list-skus --location $region --resource-type virtualMachines --query \"[?starts_with(name,'Standard_D') && length(restrictions)==\\\`0\\\`].name\" -o tsv
     Then set VM_SIZE in infra/common/env.sh (current value: $size)."

  # The availability decision is made with --query alone (no jq), so a missing jq can
  # never make this fail OPEN and wave the build through to an ARM SkuNotAvailable.
  found="$(az vm list-skus --location "$region" --resource-type virtualMachines \
            --query "length([?name=='${size}'])" -o tsv 2>/dev/null || echo "")"
  if [[ -z "$found" ]]; then
    warn "Could not query VM SKUs in $region (az error or no permission) — skipping the size preflight."
    return 0
  fi
  if [[ "$found" == "0" ]]; then
    die "VM size '$size' is NOT offered in $region.
$hint"
  fi
  restrictions="$(az vm list-skus --location "$region" --resource-type virtualMachines \
                   --query "length([?name=='${size}'] | [0].restrictions)" -o tsv 2>/dev/null || echo "0")"
  if [[ "${restrictions:-0}" != "0" ]]; then
    warn "VM size '$size' is offered in $region but is RESTRICTED for this subscription:"
    detail="$(az vm list-skus --location "$region" --resource-type virtualMachines \
               --query "[?name=='${size}'] | [0].restrictions[].{type:type,reason:reasonCode}" -o tsv 2>/dev/null || true)"
    [[ -n "$detail" ]] && printf '       %s\n' $detail
    die "Refusing to deploy: '$size' would fail ARM preflight with SkuNotAvailable / capacity restriction.
$hint"
  fi
  ok "Preflight OK: '$size' is available and unrestricted in $region."
}
_preflight_vm_size "$VM_SIZE" "$AZ_REGION"

# ---------- Deploy the VM Bicep ----------
DEPLOYMENT_NAME="phase10-vmhost-$(date -u +%Y%m%d-%H%M%S)"
log "Deploying VM Bicep (deployment: $DEPLOYMENT_NAME) ..."
az deployment group create \
  --resource-group "$AZ_RG" \
  --name "$DEPLOYMENT_NAME" \
  --template-file "$SCRIPT_DIR/main.bicep" \
  --parameters \
      location="$AZ_REGION" \
      vmName="$NAME_VM" \
      nicName="$NAME_VM_NIC" \
      nsgName="$NAME_VM_NSG" \
      vnetName="$NAME_VM_VNET" \
      vmSize="$VM_SIZE" \
      adminUsername="$VM_ADMIN_USER" \
      sshPublicKey="$SSH_PUB" \
      persistPipId="$PERSIST_PIP_ID" \
      cloudInitBase64="$CLOUD_INIT_B64" \
      uamiResourceId="$UAMI_ID" \
      projectTag="$PROJECT_TAG_VALUE" \
  --only-show-errors -o none \
  || die "VM Bicep deployment failed."
ok "VM deployed: $NAME_VM"

VM_PRINCIPAL_ID="$(az vm show -g "$AZ_RG" -n "$NAME_VM" --query identity.principalId -o tsv 2>/dev/null || true)"
# The system-assigned identity's principalId can lag a few seconds behind VM creation; retry so
# the keyless Foundry role grant below never silently skips.
if [[ -z "$VM_PRINCIPAL_ID" ]]; then
  for _i in $(seq 1 12); do
    sleep 5
    VM_PRINCIPAL_ID="$(az vm show -g "$AZ_RG" -n "$NAME_VM" --query identity.principalId -o tsv 2>/dev/null || true)"
    [[ -n "$VM_PRINCIPAL_ID" ]] && break
  done
fi
[[ -n "$VM_PRINCIPAL_ID" ]] && ok "VM managed-identity principal: $VM_PRINCIPAL_ID" \
  || warn "Could not resolve the VM managed-identity principalId — the keyless role grant may be skipped."

# ---------- SSH helpers ----------
SSH_OPTS=(-i "$SSH_KEY" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 -o LogLevel=ERROR)
# vm_ssh uses -n (stdin from /dev/null) so it never consumes this script's stdin — important
# because run_wave pipes canned answers (y/DELETE/…) into each phase's stdin. The streaming
# helpers below deliberately DO connect stdin/stdout and must NOT use -n.
vm_ssh()  { ssh -n "${SSH_OPTS[@]}" "${VM_ADMIN_USER}@${PERSIST_IP}" "$@"; }
# stream a local file to a remote command's stdin:  vm_pipe_in <localfile> <remote cmd...>
vm_pipe_in() { local f="$1"; shift; ssh "${SSH_OPTS[@]}" "${VM_ADMIN_USER}@${PERSIST_IP}" "$@" < "$f"; }
# stream a remote command's stdout into a local file:  vm_pipe_out <localfile> <remote cmd...>
vm_pipe_out() { local f="$1"; shift; ssh -n "${SSH_OPTS[@]}" "${VM_ADMIN_USER}@${PERSIST_IP}" "$@" > "$f"; }

# ---------- Wait for SSH + cloud-init completion ----------
log "Waiting for SSH on ${PERSIST_IP} ..."
for i in $(seq 1 40); do
  if vm_ssh true 2>/dev/null; then ok "SSH reachable."; break; fi
  [[ "$i" == "40" ]] && die "SSH did not come up on $PERSIST_IP after ~10 min."
  sleep 15
done
log "Waiting for cloud-init to finish (Caddy + Python deps install) ..."
vm_ssh 'sudo cloud-init status --wait' >/dev/null 2>&1 || warn "cloud-init status --wait returned non-zero (continuing; will verify below)."
for i in $(seq 1 40); do
  if vm_ssh 'test -f /opt/rmx/cloud-init-done'; then ok "cloud-init complete."; break; fi
  [[ "$i" == "40" ]] && die "cloud-init did not complete on the VM."
  sleep 15
done

# ---------- Push the real Caddyfile + systemd units ----------
# This MUST happen before Caddy is started below (the cert flow starts it), because the
# placeholder Caddyfile cloud-init wrote only knows about the static site. Caddy does not
# probe upstreams at startup, so installing the full config now — while rmx-toolapi and
# rmx-videoassist do not yet exist — is safe: /api and /video will simply 502 until the
# later phases deploy those services, while / and the ACME challenge work immediately.
log "Installing the rendered Caddyfile and systemd units on the VM ..."
vm_pipe_in "$CADDYFILE_RENDERED" 'sudo tee /etc/caddy/Caddyfile >/dev/null' \
  || die "Failed to install the Caddyfile on the VM."
vm_pipe_in "$TOOLAPI_UNIT_RENDERED" 'sudo tee /etc/systemd/system/rmx-toolapi.service >/dev/null' \
  || die "Failed to install rmx-toolapi.service on the VM."
vm_pipe_in "$VIDEOASSIST_UNIT_RENDERED" 'sudo tee /etc/systemd/system/rmx-videoassist.service >/dev/null' \
  || die "Failed to install rmx-videoassist.service on the VM."
vm_ssh 'sudo systemctl daemon-reload' || die "systemctl daemon-reload failed on the VM."
# Fail loudly here rather than at the first request: a Caddyfile typo would otherwise only
# surface as a Caddy start failure inside the cert flow, where it looks like an ACME problem.
vm_ssh 'sudo caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile' >/dev/null 2>&1 \
  || die "The rendered Caddyfile is INVALID on the VM. Run: ssh ${VM_ADMIN_USER}@${PERSIST_IP} sudo caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile"
ok "Caddyfile validated; units installed (services are deployed by later phases)."

# ---------- Secrets -> root-owned 0600 systemd EnvironmentFile ----------
# Everything the app services need that must not be public lands in ONE root-owned 0600 file.
# It is written by streaming over SSH stdin, never as a command argument: an argument would be
# visible in `ps`, in /proc/<pid>/cmdline and in any shell history on the VM. The file is not
# in the repo and not in any git-tracked path.
#
# AZURE_CLIENT_ID is the load-bearing line: the VM carries BOTH a system-assigned identity
# (for keyless gpt-5.4 generation) and the shared platform UAMI (which holds every phase2
# Foundry/Search/ACS grant). With two identities attached, DefaultAzureCredential cannot pick
# one, so this pins ManagedIdentityCredential to the UAMI — exactly what phase9 did for the
# Container App. Without it the app services would authenticate as the wrong principal and
# get 403 from Foundry.
log "Writing the root-owned 0600 EnvironmentFile ($VM_ENV_FILE) ..."
ENV_FILE_LOCAL="$SCRIPT_DIR/.rmx.env.rendered"
# umask is scoped to a subshell: setting it inline would silently tighten the permissions of
# every later file this script writes, including the exported cert tarball and outputs.env.
(
  umask 077
  {
    echo "# Contoso RM Assist — shared runtime secrets. Root-owned 0600, systemd EnvironmentFile."
    echo "# Generated by infra/phase10-vmhost/up.sh from infra/common/secrets.env. DO NOT COMMIT."
    echo "AZURE_CLIENT_ID=${UAMI_CLIENT_ID}"
    [[ -n "${ACS_CONNECTION_STRING:-}"             ]] && echo "ACS_CONNECTION_STRING=${ACS_CONNECTION_STRING}"
    [[ -n "${TOOLAPI_BEARER:-}"                    ]] && echo "TOOLAPI_BEARER=${TOOLAPI_BEARER}"
    [[ -n "${TEAMS_WEBHOOK_URL:-}"                 ]] && echo "TEAMS_WEBHOOK_URL=${TEAMS_WEBHOOK_URL}"
    [[ -n "${TEAMS_NUDGE_WEBHOOK_URL:-}"           ]] && echo "TEAMS_NUDGE_WEBHOOK_URL=${TEAMS_NUDGE_WEBHOOK_URL}"
    [[ -n "${SCHEDULE_WEBHOOK_URL:-}"              ]] && echo "SCHEDULE_WEBHOOK_URL=${SCHEDULE_WEBHOOK_URL}"
    [[ -n "${SCHEDULE_AVAILABILITY_WEBHOOK_URL:-}" ]] && echo "SCHEDULE_AVAILABILITY_WEBHOOK_URL=${SCHEDULE_AVAILABILITY_WEBHOOK_URL}"
    [[ -n "${GRAPH_TENANT_ID:-}"                   ]] && echo "GRAPH_TENANT_ID=${GRAPH_TENANT_ID}"
    [[ -n "${GRAPH_CLIENT_ID:-}"                   ]] && echo "GRAPH_CLIENT_ID=${GRAPH_CLIENT_ID}"
    [[ -n "${GRAPH_CLIENT_SECRET:-}"               ]] && echo "GRAPH_CLIENT_SECRET=${GRAPH_CLIENT_SECRET}"
    [[ -n "${RM_USER_ID:-}"                        ]] && echo "RM_USER_ID=${RM_USER_ID}"
    [[ -n "${RM_MEETING_URL:-}"                    ]] && echo "RM_MEETING_URL=${RM_MEETING_URL}"
    # The trailing `true` keeps `set -e` from killing the subshell when the LAST [[ ]] above
    # is false (an unset optional secret) — a real failure mode, not a theoretical one.
    true
  } > "$ENV_FILE_LOCAL"
)
# Create 0600 root:root FIRST, then fill it — so the values are never briefly world-readable.
vm_ssh "sudo install -o root -g root -m 0600 /dev/null ${VM_ENV_FILE}" \
  || die "Failed to create ${VM_ENV_FILE} on the VM."
vm_pipe_in "$ENV_FILE_LOCAL" "sudo tee ${VM_ENV_FILE} >/dev/null" \
  || die "Failed to write ${VM_ENV_FILE} on the VM."
vm_ssh "sudo chmod 0600 ${VM_ENV_FILE} && sudo chown root:root ${VM_ENV_FILE}"
shred -u "$ENV_FILE_LOCAL" 2>/dev/null || rm -f "$ENV_FILE_LOCAL"
ok "Secrets installed at ${VM_ENV_FILE} (root:root 0600); local copy shredded."

# ---------- Certificate: pre-seed the stored cert, else mint once and store it ----------
# The cert bundle is kept ENCRYPTED in the repo (tools/cert_store.sh). The plaintext tarball
# only ever exists as a temp file here and is shredded on the way out, so the private key is
# never written to a git-tracked path in the clear.
CERT_PLAIN_TGZ="$SCRIPT_DIR/.caddy-data.tgz"     # transient, git-ignored, shredded below
CERT_STORE="$REPO_ROOT/tools/cert_store.sh"
CADDY_DATA_PARENT="/var/lib/caddy/.local/share"
# cert_store.sh runs as a SEPARATE bash process, so it only sees exported variables. It
# records these into .cert-lock.json; without the exports the lock would say "unknown" and
# the stale-host check on the next build would silently stop working.
export PERSIST_IP ACME_CA RMASSIST_HOST LETSENCRYPT_STAGING

_shred_plain_cert() { [[ -f "$CERT_PLAIN_TGZ" ]] && { shred -u "$CERT_PLAIN_TGZ" 2>/dev/null || rm -f "$CERT_PLAIN_TGZ"; }; return 0; }
# Shred on ANY exit path, including a die() between decrypt and cleanup — otherwise a failed
# build could leave the decrypted private key sitting in the working tree.
trap _shred_plain_cert EXIT

_start_caddy() {
  vm_ssh 'sudo systemctl enable caddy >/dev/null 2>&1 || true; sudo systemctl restart caddy' \
    || die "Failed to start Caddy on the VM."
}

_verify_tls() {
  # Give Caddy a moment, then verify the site answers over HTTPS at the stable host.
  local i
  for i in $(seq 1 24); do
    if curl -fsS --max-time 10 --resolve "${RMASSIST_HOST}:443:${PERSIST_IP}" "https://${RMASSIST_HOST}/" >/dev/null 2>&1; then
      return 0
    fi
    sleep 10
  done
  return 1
}

if bash "$CERT_STORE" status >/dev/null 2>&1; then
  # A cert is stored. Check it was minted for THIS host before trusting it.
  STORED_HOST="$(bash "$CERT_STORE" lock-field fqdn 2>/dev/null || true)"
  if [[ -n "$STORED_HOST" && "$STORED_HOST" != "$RMASSIST_HOST" ]]; then
    warn "Stored cert host ($STORED_HOST) != current host ($RMASSIST_HOST) — the static IP changed."
    warn "Ignoring the stale cert; Caddy will mint a fresh one and it will be re-stored."
    MINT_FRESH=1
  elif bash "$CERT_STORE" restore "$CERT_PLAIN_TGZ"; then
    log "Pre-seeding the stored Let's Encrypt cert onto the VM (no ACME call) ..."
    vm_ssh "sudo mkdir -p ${CADDY_DATA_PARENT}"
    vm_pipe_in "$CERT_PLAIN_TGZ" "sudo tar -C ${CADDY_DATA_PARENT} -xzf - && sudo chown -R caddy:caddy ${CADDY_DATA_PARENT}/caddy" \
      || die "Failed to pre-seed the stored cert onto the VM."
    _shred_plain_cert
    _start_caddy
    if _verify_tls; then
      ok "TLS verified with the REUSED stored certificate — no Let's Encrypt call made."
      MINT_FRESH=0
    else
      warn "Serving the reused cert did not verify; falling back to minting a fresh cert."
      MINT_FRESH=1
    fi
  else
    warn "Could not decrypt the stored cert — minting a fresh one."
    MINT_FRESH=1
  fi
else
  log "No stored cert — this is the first build; Caddy will mint one via Let's Encrypt HTTP-01."
  MINT_FRESH=1
fi

if [[ "${MINT_FRESH:-0}" == "1" ]]; then
  # Fresh cert: clear any stale store, start Caddy (auto-ACME), wait for issuance, export + freeze.
  vm_ssh "sudo rm -rf ${CADDY_DATA_PARENT}/caddy" || true
  _start_caddy
  log "Waiting for Caddy to obtain the certificate from Let's Encrypt (HTTP-01) ..."
  if ! _verify_tls; then
    warn "TLS did not verify within the wait window. Check that port 80/443 on ${PERSIST_IP} are reachable"
    warn "and that ${RMASSIST_HOST} resolves to ${PERSIST_IP} (nip.io is automatic). Caddy logs:"
    vm_ssh 'sudo journalctl -u caddy --no-pager -n 40' 2>/dev/null || true
    die "Certificate issuance/verification failed on the first build."
  fi
  ok "Certificate obtained and TLS verified."
  log "Exporting the Caddy cert store from the VM and encrypting it into $CERT_DIR/ ..."
  mkdir -p "$REPO_ROOT/$CERT_DIR"
  vm_pipe_out "$CERT_PLAIN_TGZ" "sudo tar -C ${CADDY_DATA_PARENT} -czf - caddy" \
    || die "Failed to export the cert store from the VM."
  # --force: we just minted a NEW cert, so it must replace whatever was stored (this branch is
  # also reached when a stale cert for a previous host was rejected above).
  # RMASSIST_HOST / PERSIST_IP / ACME_CA / LETSENCRYPT_STAGING are already exported, and
  # cert_store.sh records them into .cert-lock.json — which is what replaced CERT_FROZEN.
  bash "$CERT_STORE" publish "$CERT_PLAIN_TGZ" --force \
    || die "Failed to encrypt + store the cert into $CERT_DIR/."
  _shred_plain_cert
  ok "Cert encrypted + stored in $CERT_DIR/. Commit it so it is reused forever (build.sh auto-commits)."
fi

# ---------- Grant the VM MSI keyless access to the gpt-5.4 Foundry account ----------
# Both roles are granted at the Foundry ACCOUNT scope: "Cognitive Services OpenAI User" is the
# data-plane role the generators actually need; "Cognitive Services User" is belt-and-braces.
# Create is idempotent; we then VERIFY at least one data-plane role is present so a silent grant
# failure surfaces here (not 10 min later in the generation preflight).
if [[ -n "$VM_PRINCIPAL_ID" ]]; then
  AISVC_ID="$(az cognitiveservices account show -n "$NAME_AISERVICES" -g "$AZ_RG" --query id -o tsv 2>/dev/null || true)"
  if [[ -z "$AISVC_ID" ]]; then
    # phase2 creates the Foundry account before phase10 runs, but be defensive under re-runs.
    for _i in $(seq 1 12); do
      sleep 5
      AISVC_ID="$(az cognitiveservices account show -n "$NAME_AISERVICES" -g "$AZ_RG" --query id -o tsv 2>/dev/null || true)"
      [[ -n "$AISVC_ID" ]] && break
    done
  fi
  if [[ -n "$AISVC_ID" ]]; then
    for role in "Cognitive Services OpenAI User" "Cognitive Services User"; do
      az role assignment create --assignee-object-id "$VM_PRINCIPAL_ID" --assignee-principal-type ServicePrincipal \
        --role "$role" --scope "$AISVC_ID" -o none 2>/dev/null \
        && ok "Granted VM MSI '$role' on $NAME_AISERVICES" \
        || log "VM MSI '$role' on $NAME_AISERVICES already present or not grantable now (continuing)."
    done
    GRANTED_ROLES="$(az role assignment list --assignee "$VM_PRINCIPAL_ID" --scope "$AISVC_ID" \
      --query "[].roleDefinitionName" -o tsv 2>/dev/null || true)"
    if printf '%s\n' "$GRANTED_ROLES" | grep -qiE 'Cognitive Services (OpenAI )?User'; then
      ok "Verified keyless Foundry role(s) on the VM MSI: $(printf '%s' "$GRANTED_ROLES" | tr '\n' ',' | sed 's/,$//')"
    else
      warn "Could NOT verify a Cognitive Services data-plane role on the VM MSI. Keyless generation"
      warn "will likely fail. Ensure you can create role assignments on $NAME_AISERVICES, then re-run."
    fi
  else
    warn "Foundry account $NAME_AISERVICES not found — cannot grant the VM MSI keyless access."
    warn "Run phase2-ai first (it creates the gpt-5.4 Foundry account), then re-run this phase."
  fi
else
  warn "No VM managed-identity principalId — skipping the keyless Foundry role grant (generation will fail)."
fi

# ---------- Outputs ----------
OUTFILE="$SCRIPT_DIR/outputs.env"
cat > "$OUTFILE" <<EOF
# Generated by infra/phase10-vmhost/up.sh on $(date -u --iso-8601=seconds)
export VM_HOST_IP="$PERSIST_IP"
export RMASSIST_HOST="$RMASSIST_HOST"
export VM_SSH_KEY="$SSH_KEY"
export VM_ADMIN_USER="$VM_ADMIN_USER"
export VM_APP_USER="$VM_APP_USER"
export VM_PRINCIPAL_ID="$VM_PRINCIPAL_ID"
export RMASSIST_URL="https://$RMASSIST_HOST/"
# Public, browser-facing URLs. Everything is same-origin now: one host, one cert, three
# path prefixes. Later phases consume these instead of Container App FQDNs, and the CRM
# cockpit is injected with them at deploy time.
export CRM_BASE_URL="https://$RMASSIST_HOST"
export CRM_ALLOWED_ORIGIN="https://$RMASSIST_HOST"
export TOOLAPI_URL="https://$RMASSIST_HOST$TOOLAPI_PATH_PREFIX"
export VIDEOASSIST_URL="https://$RMASSIST_HOST$VIDEOASSIST_PATH_PREFIX"
EOF
rm -f "$CLOUD_INIT_RENDERED" 2>/dev/null || true

cat <<EOF

$(printf '\033[1;36m================ Phase 10 — application host ready ================\033[0m')
VM:                 $NAME_VM  ($VM_SIZE, $AZ_REGION)
Public IP:          $PERSIST_IP   (persistent — borrowed from $AZ_RG_PERSISTENT)
Stable host:        https://$RMASSIST_HOST/
Certificate:        $([ "${MINT_FRESH:-0}" == "1" ] && echo "MINTED via Let's Encrypt + exported to $CERT_DIR (commit it)" || echo "REUSED from committed $CERT_DIR (no LE call)")
Routing (Caddy):    /         -> $VM_WEB_DIR                (CRM cockpit, static)
                    /api/*    -> $TOOLAPI_BIND      (rmx-toolapi.service)
                    /video/*  -> $VIDEOASSIST_BIND      (rmx-videoassist.service)
Units installed:    rmx-toolapi.service, rmx-videoassist.service (started by later phases)
Secrets:            $VM_ENV_FILE  (root:root 0600)
VM identity:        $VM_PRINCIPAL_ID  (keyless gpt-5.4 access)
UAMI attached:      $UAMI_CLIENT_ID  (Foundry/Search/ACS grants from phase2)
Generation:         run on the VM by tools/run-generation-on-vm.sh (keyless MSI)
$(printf '\033[1;36m===================================================================\033[0m')
EOF
ok "Phase 10 complete."
