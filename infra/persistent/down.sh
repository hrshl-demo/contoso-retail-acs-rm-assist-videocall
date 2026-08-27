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

# ---------------------------------------------------------------------------------------
# ALWAYS delete OUR public IP by name — never the resource group by default.
#
# $AZ_RG_PERSISTENT may be a SHARED edge RG holding static IPs for several unrelated demos
# (it currently also carries secamc-deploy-pip and setu-deploy-pip). Deleting the group
# would silently destroy those other projects' reserved addresses, and each of them anchors
# its own committed TLS certificate — so the blast radius is other people's demos, not just
# this one. Removing only $NAME_PERSIST_PIP is correct whether the RG is dedicated or shared.
# ---------------------------------------------------------------------------------------
if az network public-ip show -g "$AZ_RG_PERSISTENT" -n "$NAME_PERSIST_PIP" -o none 2>/dev/null; then
  warn "Deleting the static public IP '$NAME_PERSIST_PIP' — the stable hostname will be lost."
  if az network public-ip delete -g "$AZ_RG_PERSISTENT" -n "$NAME_PERSIST_PIP" -o none; then
    ok "Static public IP '$NAME_PERSIST_PIP' deleted."
  else
    warn "Public IP delete reported an error — check the portal."
  fi
else
  ok "Static public IP '$NAME_PERSIST_PIP' not present in '$AZ_RG_PERSISTENT' — nothing to delete."
fi

# Only consider removing the RG itself when it is unmistakably OURS *and* now empty.
# Both conditions are required: a shared RG fails the tag test, and a dedicated RG that
# still holds anything else is left alone rather than guessed about.
REMAINING="$(az resource list -g "$AZ_RG_PERSISTENT" --query 'length(@)' -o tsv 2>/dev/null || echo "?")"
if [[ "$RG_TAG_VALUE" == "$PROJECT_TAG_VALUE_PERSISTENT" && "$REMAINING" == "0" ]]; then
  log "Persistent RG '$AZ_RG_PERSISTENT' is ours and now empty — removing it too."
  az group delete --name "$AZ_RG_PERSISTENT" --yes -o none \
    && ok "Persistent RG '$AZ_RG_PERSISTENT' deleted." \
    || warn "Persistent RG delete reported an error — check the portal."
elif [[ "$RG_TAG_VALUE" != "$PROJECT_TAG_VALUE_PERSISTENT" ]]; then
  ok "Leaving RG '$AZ_RG_PERSISTENT' in place — it is shared (not tagged ${PROJECT_TAG_KEY}=${PROJECT_TAG_VALUE_PERSISTENT})."
else
  ok "Leaving RG '$AZ_RG_PERSISTENT' in place — it still holds ${REMAINING} other resource(s)."
fi
rm -f "$SCRIPT_DIR/outputs.env" 2>/dev/null || true
ok "Persistent teardown complete. The stored cert in $CERT_DIR is now stale; delete $CERT_ENC_FILE + $CERT_LOCK_FILE to re-mint on the next build."
