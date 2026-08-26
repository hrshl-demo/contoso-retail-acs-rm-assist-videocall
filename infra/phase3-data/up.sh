#!/usr/bin/env bash
# infra/phase3-data/up.sh
#
# Phase 3 — Synthetic retail data validation. Creates NO Azure resources.
#
# The deterministic CSV pack (data/csv/), the curated knowledge base
# (data/knowledge_base/) and the SOP prose corpus (docs/sop/*.md) are committed
# to this repository, so the demo is fully self-contained and reproducible.
# This phase proves that committed pack is internally consistent (blueprint 6.3)
# and halts the rebuild if anything drifted.
set -euo pipefail
PHASE="phase3"; export PHASE
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=../common/env.sh
source "$SCRIPT_DIR/../common/env.sh"

log "Phase 3 — Retail synthetic data validation"
cd "$REPO_ROOT"

# ---------- Guard: committed data pack must be present ----------
CSV_COUNT="$( { find data/csv -name '*.csv' 2>/dev/null || true; } | wc -l | tr -d ' ')"
[[ "$CSV_COUNT" == "0" ]] && die "No CSVs found under data/csv. The data pack ships with this repo — ensure data/csv was included."
ok "Found $CSV_COUNT committed CSV file(s)."

# ---------- OPT-IN regeneration (off by default) ----------
# Deploys must be reproducible and offline, so the COMMITTED pack is the default
# source of truth and this phase only validates it. Regeneration is a deliberate,
# explicit act:
#
#   REGENERATE_SEED=1 bash infra/phase3-data/up.sh              # 1 customer (Rakesh only)
#   REGENERATE_SEED=1 SEED_CUSTOMERS=3 bash infra/phase3-data/up.sh
#
# The generator is fixed-seed, stdlib-only and makes no network calls, so the
# result is byte-identical for the same inputs. Rakesh (CTB-RTL-002) and every
# invariant his demo narrative depends on are preserved regardless of count.
if [[ "${REGENERATE_SEED:-0}" == "1" ]]; then
  warn "REGENERATE_SEED=1 — regenerating data/csv from infra/phase3-data/generate_seed.py."
  warn "This OVERWRITES the committed pack. Review 'git diff data/' before committing."
  python3 "$SCRIPT_DIR/generate_seed.py" \
    --customers "${SEED_CUSTOMERS:-1}" \
    --seed "${SEED_STRING:-contoso-retail-v2}" \
    || die "Seed generation failed."
  ok "Regenerated the retail pack (${SEED_CUSTOMERS:-1} customer(s))."
else
  log "Using the committed data pack (set REGENERATE_SEED=1 to regenerate)."
fi

# ---------- Validate internal consistency (deterministic source of truth) ----------
log "Validating internal consistency (blueprint 6.3) ..."
python3 "$SCRIPT_DIR/validate_seed.py"

ok "Phase 3 complete. Data pack is consistent and locked."
log "Consumed later by: Phase 4 (ingest into SQLite), Phase 5 (RAG indexes docs/sop + knowledge_base)."
log "Next: bash infra/phase4-toolapi/up.sh"
