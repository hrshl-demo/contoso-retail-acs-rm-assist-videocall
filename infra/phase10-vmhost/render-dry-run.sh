#!/usr/bin/env bash
# Dry-run the phase10 template rendering with representative values, exactly as up.sh does,
# so the resulting Caddyfile + systemd units can be reviewed without deploying anything.
set -euo pipefail
cd "$(dirname "$0")"

RMASSIST_HOST="rmassist.4.240.103.77.nip.io"
LETSENCRYPT_EMAIL="rmassist-demo@example.com"
ACME_CA="https://acme-v02.api.letsencrypt.org/directory"
VM_ADMIN_USER="azureuser"
VM_APP_USER="rmxapp"
VM_APP_ROOT="/opt/rmx"
VM_TOOLAPI_DIR="$VM_APP_ROOT/toolapi"
VM_VIDEOASSIST_DIR="$VM_APP_ROOT/videoassist"
VM_ENV_FILE="$VM_APP_ROOT/etc/rmx.env"
VM_TOOLAPI_ENV_FILE="$VM_APP_ROOT/etc/toolapi.env"
VM_VIDEOASSIST_ENV_FILE="$VM_APP_ROOT/etc/videoassist.env"
VM_NODE_BIN="/usr/bin/node"
TOOLAPI_HOST="127.0.0.1";     TOOLAPI_PORT="8000"
VIDEOASSIST_HOST="127.0.0.1"; VIDEOASSIST_PORT="3000"
TOOLAPI_BIND="$TOOLAPI_HOST:$TOOLAPI_PORT"
VIDEOASSIST_BIND="$VIDEOASSIST_HOST:$VIDEOASSIST_PORT"

banner() { printf '\n=========== %s ===========\n' "$1"; }

banner "RENDERED /etc/caddy/Caddyfile"
sed -e "s#__RMASSIST_HOST__#${RMASSIST_HOST}#g" \
    -e "s#__LETSENCRYPT_EMAIL__#${LETSENCRYPT_EMAIL}#g" \
    -e "s#__ACME_CA__#${ACME_CA}#g" \
    -e "s#__TOOLAPI_BIND__#${TOOLAPI_BIND}#g" \
    -e "s#__VIDEOASSIST_BIND__#${VIDEOASSIST_BIND}#g" \
    Caddyfile.tmpl

banner "RENDERED /etc/systemd/system/rmx-toolapi.service"
sed -e "s#__APP_USER__#${VM_APP_USER}#g" \
    -e "s#__TOOLAPI_DIR__#${VM_TOOLAPI_DIR}#g" \
    -e "s#__TOOLAPI_HOST__#${TOOLAPI_HOST}#g" \
    -e "s#__TOOLAPI_PORT__#${TOOLAPI_PORT}#g" \
    -e "s#__ENV_FILE__#${VM_ENV_FILE}#g" \
    -e "s#__APP_ENV_FILE__#${VM_TOOLAPI_ENV_FILE}#g" \
    rmx-toolapi.service.tmpl

banner "RENDERED /etc/systemd/system/rmx-videoassist.service"
sed -e "s#__APP_USER__#${VM_APP_USER}#g" \
    -e "s#__VIDEOASSIST_DIR__#${VM_VIDEOASSIST_DIR}#g" \
    -e "s#__VIDEOASSIST_HOST__#${VIDEOASSIST_HOST}#g" \
    -e "s#__VIDEOASSIST_PORT__#${VIDEOASSIST_PORT}#g" \
    -e "s#__NODE_BIN__#${VM_NODE_BIN}#g" \
    -e "s#__ENV_FILE__#${VM_ENV_FILE}#g" \
    -e "s#__APP_ENV_FILE__#${VM_VIDEOASSIST_ENV_FILE}#g" \
    rmx-videoassist.service.tmpl

banner "RENDERED cloud-init (runcmd + write_files targets only)"
sed -e "s#__RMASSIST_HOST__#${RMASSIST_HOST}#g" \
    -e "s#__LETSENCRYPT_EMAIL__#${LETSENCRYPT_EMAIL}#g" \
    -e "s#__ACME_CA__#${ACME_CA}#g" \
    -e "s#__ADMIN_USER__#${VM_ADMIN_USER}#g" \
    -e "s#__APP_USER__#${VM_APP_USER}#g" \
    cloud-init.yaml | sed -n '/^runcmd:/,$p'

banner "UNSUBSTITUTED TOKENS LEFT ANYWHERE (must be empty)"
for f in Caddyfile.tmpl rmx-toolapi.service.tmpl rmx-videoassist.service.tmpl cloud-init.yaml; do
  case "$f" in
    Caddyfile.tmpl)
      out=$(sed -e "s#__RMASSIST_HOST__##g;s#__LETSENCRYPT_EMAIL__##g;s#__ACME_CA__##g;s#__TOOLAPI_BIND__##g;s#__VIDEOASSIST_BIND__##g" "$f" | grep -n '__[A-Z_]*__' || true) ;;
    rmx-toolapi.service.tmpl)
      out=$(sed -e "s#__APP_USER__##g;s#__TOOLAPI_DIR__##g;s#__TOOLAPI_HOST__##g;s#__TOOLAPI_PORT__##g;s#__ENV_FILE__##g;s#__APP_ENV_FILE__##g" "$f" | grep -n '__[A-Z_]*__' || true) ;;
    rmx-videoassist.service.tmpl)
      out=$(sed -e "s#__APP_USER__##g;s#__VIDEOASSIST_DIR__##g;s#__VIDEOASSIST_HOST__##g;s#__VIDEOASSIST_PORT__##g;s#__NODE_BIN__##g;s#__ENV_FILE__##g;s#__APP_ENV_FILE__##g" "$f" | grep -n '__[A-Z_]*__' || true) ;;
    cloud-init.yaml)
      out=$(sed -e "s#__RMASSIST_HOST__##g;s#__LETSENCRYPT_EMAIL__##g;s#__ACME_CA__##g;s#__ADMIN_USER__##g;s#__APP_USER__##g" "$f" | grep -n '__[A-Z_]*__' || true) ;;
  esac
  if [[ -n "$out" ]]; then echo "LEFTOVER in $f:"; echo "$out"; else echo "OK  $f — no leftover tokens"; fi
done
