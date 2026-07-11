#!/usr/bin/env bash
# infra/phase4-toolapi/down.sh
# Phase 4 teardown — deletes the Tool API Container App (tag-guarded) and the
# Phase 4 KV secret. Does NOT touch Phase 1/2 resources.
set -uo pipefail
PHASE="phase4"; export PHASE
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../common/env.sh
source "$SCRIPT_DIR/../common/env.sh"

log "Phase 4 — Teardown"
ensure_az_login
ensure_rg

APP_ID="$(az containerapp show -g "$AZ_RG" -n "$NAME_CA_TOOLAPI" --query id -o tsv 2>/dev/null || true)"
if [[ -n "$APP_ID" ]]; then
  assert_project_tag "$APP_ID"
  log "Deleting Container App: $NAME_CA_TOOLAPI"
  az containerapp delete -g "$AZ_RG" -n "$NAME_CA_TOOLAPI" --yes --only-show-errors
  ok "Deleted: $NAME_CA_TOOLAPI"
else
  warn "Not found (skipping): $NAME_CA_TOOLAPI"
fi

rm -f "$SCRIPT_DIR/outputs.env"
ok "Phase 4 teardown complete."
