#!/usr/bin/env bash
# infra/phase3-data/down.sh
#
# Phase 3 — Teardown. Phase 3 creates NO Azure resources. The CSV pack (data/csv/),
# the knowledge base (data/knowledge_base/) and the SOP corpus (docs/sop/*.md) are
# COMMITTED, deterministic artifacts that SHIP with this repo — they are the source
# of truth that phase3-data/up.sh VALIDATES (it does NOT regenerate them). Teardown
# therefore PRESERVES them; deleting data/csv here would leave a rebuild-after-wipe
# with no data pack to validate, silently failing the preflight gate.
set -uo pipefail
PHASE="phase3"; export PHASE
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=../common/env.sh
source "$SCRIPT_DIR/../common/env.sh"

log "Phase 3 — Teardown (no-op: committed data pack is preserved)"
log "Preserved: data/csv/*, data/knowledge_base/* and docs/sop/*.md (committed, deterministic artifacts)."
ok "Phase 3 teardown complete."
