#!/usr/bin/env bash
# infra/phase1-platform/down.sh
#
# Phase 1 — Safe teardown.
# Deletes ONLY resources tagged project=contoso-msme-rm-assist and only the specific
# names this phase creates. Refuses to touch anything else.
#
# Order matters:
#   1. Container Apps Environment (has dependents in later phases - error if any exist)
#   2. ACR
#   3. Log Analytics
#   4. UAMI
# Role assignments cascade with their scope resource, so we don't need to delete them explicitly.

set -euo pipefail
PHASE="phase1"
export PHASE

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../common/env.sh
source "$SCRIPT_DIR/../common/env.sh"

log "Phase 1 — Teardown"
ensure_az_login
ensure_rg

cat <<EOF

$(printf '\033[1;33m================ The following WILL be deleted ================\033[0m')
  Log Analytics:              $NAME_LAW
  Managed Identity:           $NAME_UAMI

Each resource will be verified for tag '${PROJECT_TAG}' BEFORE deletion.
$(printf '\033[1;33m===============================================================\033[0m')

EOF
confirm "Proceed with Phase 1 teardown?"

# ---------- Resolve IDs + verify project tag UP-FRONT (fast, sequential, safe) ----------
# Tag assertions happen before ANY delete, so parallel deletion never touches an untagged
# resource.
log "Resolving Phase 1 resources and verifying project tag before deletion..."
LAW_ID="$(az monitor log-analytics workspace show -g "$AZ_RG" -n "$NAME_LAW" --query id -o tsv 2>/dev/null || true)"
[[ -n "$LAW_ID" ]] && assert_project_tag "$LAW_ID"
UAMI_RID="$(az identity show -g "$AZ_RG" -n "$NAME_UAMI" --query id -o tsv 2>/dev/null || true)"
[[ -n "$UAMI_RID" ]] && assert_project_tag "$UAMI_RID"

# ---------- Per-resource delete functions (each self-contained + logged) ----------
_del_law() {
  [[ -n "$LAW_ID" ]] || { warn "Not found (skipping): $NAME_LAW"; return 0; }
  log "Deleting Log Analytics workspace: $NAME_LAW (--force, skip soft-delete)..."
  az monitor log-analytics workspace delete -g "$AZ_RG" -n "$NAME_LAW" --yes --force true --only-show-errors \
    && ok "Deleted: $NAME_LAW" || warn "Log Analytics delete failed: $NAME_LAW"
}
_del_uami() {
  [[ -n "$UAMI_RID" ]] || { warn "Not found (skipping): $NAME_UAMI"; return 0; }
  log "Deleting UAMI: $NAME_UAMI ..."
  az identity delete -g "$AZ_RG" -n "$NAME_UAMI" --only-show-errors \
    && ok "Deleted: $NAME_UAMI" || warn "UAMI delete failed: $NAME_UAMI"
}

# ---------- Delete ----------
# The slow Container Apps Environment delete is gone, so this is now quick either way.
if [[ "${WIPE_PARALLEL_DELETES:-1}" == "1" ]]; then
  log "Deleting Phase 1 resources IN PARALLEL (WIPE_PARALLEL_DELETES=1)..."
  _del_law &  PID_LAW=$!
  _del_uami & PID_UAMI=$!
  RC_ANY=0
  for pid in "$PID_LAW" "$PID_UAMI"; do
    wait "$pid" || RC_ANY=1
  done
  [[ "$RC_ANY" -eq 0 ]] && ok "Phase 1 parallel deletes finished." \
    || warn "Some Phase 1 deletes reported issues (see above; wipe is best-effort)."
else
  log "Deleting Phase 1 resources sequentially (WIPE_PARALLEL_DELETES=0)..."
  _del_law; _del_uami
fi

# ----- Clean up outputs file -----
rm -f "$SCRIPT_DIR/outputs.env"

ok "Phase 1 teardown complete."
