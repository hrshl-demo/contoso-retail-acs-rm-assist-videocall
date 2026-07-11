#!/usr/bin/env bash
# infra/phase6-crm/down.sh — delete the dashboard Container App (tag-guarded).
set -uo pipefail
PHASE="phase6"; export PHASE
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../common/env.sh
source "$SCRIPT_DIR/../common/env.sh"
log "Phase 6 — Teardown"
ensure_az_login; ensure_rg
APP_ID="$(az containerapp show -g "$AZ_RG" -n "$NAME_CA_CRM" --query id -o tsv 2>/dev/null || true)"
if [[ -n "$APP_ID" ]]; then
  assert_project_tag "$APP_ID"
  az containerapp delete -g "$AZ_RG" -n "$NAME_CA_CRM" --yes --only-show-errors
  ok "Deleted: $NAME_CA_CRM"
else
  warn "Not found (skipping): $NAME_CA_CRM"
fi
rm -f "$SCRIPT_DIR/outputs.env"
ok "Phase 6 teardown complete."
