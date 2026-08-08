#!/usr/bin/env bash
# infra/persistent/down.sh
#
# DESTROY the persistent layer (static IP + persistent RG). This is DELIBERATELY NOT part
# of wipe.sh — deleting the static IP changes the nip.io hostname and INVALIDATES the
# committed Let's Encrypt certificate, forcing a fresh (rate-limited) HTTP-01 challenge on
# the next build. Only run this if you are permanently decommissioning the demo.
#
# Guarded: refuses to delete a persistent RG that does not carry the persistent project tag.
set -uo pipefail
PHASE="persistent"; export PHASE

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../common/env.sh
source "$SCRIPT_DIR/../common/env.sh"

log "Persistent layer — teardown (this INVALIDATES the committed cert's hostname)"
ensure_az_login

if ! az group show --name "$AZ_RG_PERSISTENT" -o none 2>/dev/null; then
  ok "Persistent RG '$AZ_RG_PERSISTENT' not found — nothing to delete."
  exit 0
fi

RG_TAG_VALUE="$(az group show --name "$AZ_RG_PERSISTENT" --query "tags.${PROJECT_TAG_KEY}" -o tsv 2>/dev/null || true)"
if [[ "${PERSIST_FORCE:-0}" != "1" && "$RG_TAG_VALUE" != "$PROJECT_TAG_VALUE_PERSISTENT" ]]; then
  warn "Persistent RG '$AZ_RG_PERSISTENT' is not tagged ${PROJECT_TAG_KEY}=${PROJECT_TAG_VALUE_PERSISTENT} (found '${RG_TAG_VALUE:-<none>}')."
  die  "Refusing to delete. Re-run with PERSIST_FORCE=1 to override."
fi

warn "Deleting the persistent RG '$AZ_RG_PERSISTENT' — the static IP and stable hostname will be lost."
if az group delete --name "$AZ_RG_PERSISTENT" --yes -o none; then
  ok "Persistent RG '$AZ_RG_PERSISTENT' deleted."
else
  warn "Persistent RG delete reported an error — check the portal."
fi
rm -f "$SCRIPT_DIR/outputs.env" 2>/dev/null || true
ok "Persistent teardown complete. The committed cert in $CERT_DIR is now stale; delete it + $CERT_FROZEN_SENTINEL to re-mint on the next build."
