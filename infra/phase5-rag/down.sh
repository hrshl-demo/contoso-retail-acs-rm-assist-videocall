#!/usr/bin/env bash
# infra/phase5-rag/down.sh
# Phase 5 teardown — drops the AI Search INDEX (not the Search service, which is
# Phase 2). The SOP source files (docs/sop) are committed artifacts and are NOT
# deleted. The Tool API image rolls back naturally on a Phase 4 rebuild.
set -uo pipefail
PHASE="phase5"; export PHASE
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../common/env.sh
source "$SCRIPT_DIR/../common/env.sh"

log "Phase 5 — Teardown (drop Search index only)"
ensure_az_login
ensure_rg

PHASE2_OUT="$SCRIPT_DIR/../phase2-ai/outputs.env"
if [[ -f "$PHASE2_OUT" ]]; then
  # shellcheck disable=SC1090
  source "$PHASE2_OUT"
fi

if [[ -n "${SEARCH_ENDPOINT:-}" ]]; then
  log "Deleting index '$SEARCH_INDEX_NAME' via REST (Entra token)..."
  TOKEN="$(az account get-access-token --resource https://search.azure.com --query accessToken -o tsv 2>/dev/null || true)"
  if [[ -n "$TOKEN" ]]; then
    curl -fsS -X DELETE \
      -H "Authorization: Bearer $TOKEN" \
      "${SEARCH_ENDPOINT%/}/indexes/${SEARCH_INDEX_NAME}?api-version=2024-07-01" \
      && ok "Index dropped: $SEARCH_INDEX_NAME" \
      || warn "Index delete returned non-zero (may not exist)."
  else
    warn "Could not get Search token; index may remain. Search service teardown is Phase 2."
  fi
else
  warn "SEARCH_ENDPOINT unknown (Phase 2 outputs missing); skipping index drop."
fi

rm -f "$SCRIPT_DIR/outputs.env"
log "Preserved: docs/sop/*.md (committed one-time Foundry artifacts)."
ok "Phase 5 teardown complete."
