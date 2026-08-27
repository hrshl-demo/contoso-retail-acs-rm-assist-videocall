#!/usr/bin/env bash
# infra/phase10-vmhost/up.sh
#
# Phase 10 — Data-generation + Caddy/TLS VM (billable; deleted on wipe).
#
# Creates the Ubuntu VM, associates the PERSISTENT static IP, and manages the Let's Encrypt
# certificate with a "mint once, reuse forever" policy:
#   • If infra/cert/caddy-data.tgz (committed) exists  -> PRE-SEED it onto the VM and start
#     Caddy, which serves the existing cert WITHOUT any Let's Encrypt call.
#   • Otherwise (very first build)                     -> start Caddy, let it obtain the cert
#     via HTTP-01, then EXPORT the cert store back to infra/cert/ and write CERT_FROZEN so the
#     next build (and everyone who pulls the repo) reuses it.
# Finally it grants the VM's system-assigned managed identity keyless access to the gpt-5.4
# Foundry account, so generation on the VM needs no key.
set -euo pipefail
PHASE="phase10"; export PHASE

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=../common/env.sh
source "$SCRIPT_DIR/../common/env.sh"

log "Phase 10 — data-gen + Caddy/TLS VM"
ensure_az_login
ensure_rg

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
    "$SCRIPT_DIR/cloud-init.yaml" > "$CLOUD_INIT_RENDERED"
CLOUD_INIT_B64="$(base64 -w0 "$CLOUD_INIT_RENDERED" 2>/dev/null || base64 "$CLOUD_INIT_RENDERED" | tr -d '\n')"

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

# ---------- Certificate: pre-seed committed cert, else mint once and export ----------
CERT_TGZ="$REPO_ROOT/$CERT_DIR/caddy-data.tgz"
CERT_FROZEN_FILE="$REPO_ROOT/$CERT_FROZEN_SENTINEL"
CADDY_DATA_PARENT="/var/lib/caddy/.local/share"

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

if [[ -f "$CERT_TGZ" && -f "$CERT_FROZEN_FILE" ]]; then
  # Reuse path — NO Let's Encrypt call.
  FROZEN_HOST="$(grep -E '^CERT_HOST=' "$CERT_FROZEN_FILE" 2>/dev/null | cut -d'"' -f2 || true)"
  if [[ -n "$FROZEN_HOST" && "$FROZEN_HOST" != "$RMASSIST_HOST" ]]; then
    warn "Committed cert host ($FROZEN_HOST) != current host ($RMASSIST_HOST) — the static IP changed."
    warn "Ignoring the stale committed cert; Caddy will mint a fresh one and it will be re-exported."
    MINT_FRESH=1
  else
    log "Pre-seeding the committed Let's Encrypt cert onto the VM (no ACME call) ..."
    vm_ssh "sudo mkdir -p ${CADDY_DATA_PARENT}"
    vm_pipe_in "$CERT_TGZ" "sudo tar -C ${CADDY_DATA_PARENT} -xzf - && sudo chown -R caddy:caddy ${CADDY_DATA_PARENT}/caddy" \
      || die "Failed to pre-seed the committed cert store onto the VM."
    _start_caddy
    if _verify_tls; then
      ok "TLS verified with the REUSED committed certificate — no Let's Encrypt call made."
      MINT_FRESH=0
    else
      warn "Serving the reused cert did not verify; falling back to minting a fresh cert."
      MINT_FRESH=1
    fi
  fi
else
  log "No committed cert found — this is the first build; Caddy will mint one via Let's Encrypt HTTP-01."
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
  log "Exporting the Caddy cert store back to the repo ($CERT_DIR/caddy-data.tgz) for reuse ..."
  mkdir -p "$REPO_ROOT/$CERT_DIR"
  vm_pipe_out "$CERT_TGZ" "sudo tar -C ${CADDY_DATA_PARENT} -czf - caddy" \
    || die "Failed to export the cert store from the VM."
  cat > "$CERT_FROZEN_FILE" <<EOF
# Contoso Bank RM Assist — frozen Let's Encrypt certificate store.
# Written by infra/phase10-vmhost/up.sh. COMMIT this file AND caddy-data.tgz: their presence
# makes every future build REUSE this certificate instead of calling Let's Encrypt again
# (which avoids the LE rate limits). Regenerate only by deleting both files.
CERT_FROZEN_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
CERT_HOST="$RMASSIST_HOST"
CERT_STATIC_IP="$PERSIST_IP"
CERT_ACME_CA="$ACME_CA"
CERT_STAGING="$LETSENCRYPT_STAGING"
EOF
  ok "Cert exported + frozen. Commit infra/cert/ so it is reused forever (build.sh auto-commits it)."
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
export VM_PRINCIPAL_ID="$VM_PRINCIPAL_ID"
export RMASSIST_URL="https://$RMASSIST_HOST/"
EOF
rm -f "$CLOUD_INIT_RENDERED" 2>/dev/null || true

cat <<EOF

$(printf '\033[1;36m================ Phase 10 — data-gen + Caddy VM ready ================\033[0m')
VM:                 $NAME_VM  ($VM_SIZE, $AZ_REGION)
Public IP:          $PERSIST_IP   (persistent — borrowed from $AZ_RG_PERSISTENT)
Stable host:        https://$RMASSIST_HOST/
Certificate:        $([ "${MINT_FRESH:-0}" == "1" ] && echo "MINTED via Let's Encrypt + exported to $CERT_DIR (commit it)" || echo "REUSED from committed $CERT_DIR (no LE call)")
VM identity:        $VM_PRINCIPAL_ID  (keyless gpt-5.4 access)
Generation:         run on the VM by tools/run-generation-on-vm.sh (keyless MSI)
$(printf '\033[1;36m=====================================================================\033[0m')
EOF
ok "Phase 10 complete."
