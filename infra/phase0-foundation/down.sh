#!/usr/bin/env bash
# infra/phase0-foundation/down.sh
# Phase 0 created no billable resources. Intentional no-op (RPs stay registered).
set -euo pipefail
PHASE="phase0"; export PHASE
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../common/env.sh
source "$SCRIPT_DIR/../common/env.sh"
log "Phase 0 down — nothing to delete. Resource providers remain registered (no cost)."
ok "Done."
