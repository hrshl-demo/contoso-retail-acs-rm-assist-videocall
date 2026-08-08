#!/usr/bin/env bash
# tools/ensure-baseline.sh
#
# ONE-TIME freeze of the Contoso Bank synthetic dataset + the gpt-5.4-authored SOP corpus,
# with a sentinel guard so it runs exactly once and is a no-op on every later build.
#
# WHY
#   Every NUMBER is deterministic (generate_contosobank.py), but the NARRATIVE text — advisor
#   briefs, interaction notes, emails, meeting summaries, complaint text, opportunity reasons,
#   covenant/collateral prose — and the SOP corpus (docs/sop/contosobank_*.md) must be authored
#   by gpt-5.4 on the FIRST build. gpt-5.4 only exists after phase2, and generation runs ON THE
#   VM (keyless via its managed identity). Once generated we commit the result ("freeze") and
#   every future build reuses those exact files.
#
# BEHAVIOUR
#   • If data/contosobank/BASELINE_FROZEN exists (and schema matches) -> SKIP.
#   • Otherwise (first build, or --force):
#       1. python3 data/contosobank/generate_contosobank.py   (deterministic numbers)
#       2. python3 data/contosobank/enrich_contosobank.py     (gpt-5.4 narrative; --offline = templated)
#       3. python3 tools/generate_sops.py                     (gpt-5.4 SOPs;      --offline = templated)
#     then writes the BASELINE_FROZEN sentinel.
#
# The default path REQUIRES Azure OpenAI (so the first freeze is genuinely gpt-5.4-authored):
# it needs FOUNDRY_AOAI_ENDPOINT (exported by tools/run-generation-on-vm.sh from phase2 outputs)
# and errors out if it is missing. Pass --offline to deliberately freeze a templated baseline.
#
# Usage:
#   bash tools/ensure-baseline.sh            # first build: gpt-5.4 freeze (needs Foundry up)
#   bash tools/ensure-baseline.sh --offline  # freeze a templated (non-AI) baseline
#   bash tools/ensure-baseline.sh --force    # regenerate + re-freeze even if already frozen
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-python3}"
DATA_DIR="data/contosobank"
SENTINEL="$DATA_DIR/BASELINE_FROZEN"
DATASET_JSON="$DATA_DIR/contosobank_dataset.json"
GENERATE_PY="$DATA_DIR/generate_contosobank.py"
ENRICH_PY="$DATA_DIR/enrich_contosobank.py"
SOP_PY="tools/generate_sops.py"
SOP_DIR="docs/sop"
# Bump when the generated data SHAPE changes so an already-frozen baseline auto-regenerates.
DATA_SCHEMA_VERSION="1"

_c(){ printf '\033[%sm' "$1"; }
log(){  printf '%s[baseline]%s %s\n' "$(_c '1;34')" "$(_c 0)" "$*"; }
ok(){   printf '%s[baseline]%s %s\n' "$(_c '1;32')" "$(_c 0)" "$*"; }
warn(){ printf '%s[baseline]%s %s\n' "$(_c '1;33')" "$(_c 0)" "$*" >&2; }
die(){  printf '%s[baseline]%s %s\n' "$(_c '1;31')" "$(_c 0)" "$*" >&2; exit 1; }

FORCE=0; OFFLINE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force)   FORCE=1 ;;
    --offline) OFFLINE=1 ;;
    -h|--help) sed -n '2,34p' "$0"; exit 0 ;;
    *)         die "Unknown argument: $1 (see --help)" ;;
  esac
  shift
done

[[ -f "$GENERATE_PY" ]] || die "Missing $GENERATE_PY — the dataset generator must ship in the repo."
[[ -f "$ENRICH_PY"   ]] || die "Missing $ENRICH_PY — the enrichment pass must ship in the repo."
[[ -f "$SOP_PY"      ]] || die "Missing $SOP_PY — the SOP generator must ship in the repo."

# ---------- Freeze guard ----------
if [[ -f "$SENTINEL" && "$FORCE" != "1" ]]; then
  # shellcheck disable=SC1090
  source "$SENTINEL" 2>/dev/null || true
  if [[ "${BASELINE_SCHEMA_VERSION:-0}" != "$DATA_SCHEMA_VERSION" ]]; then
    warn "Frozen baseline schema v${BASELINE_SCHEMA_VERSION:-0} != current v$DATA_SCHEMA_VERSION — regenerating + re-freezing."
  else
    ok "Baseline already frozen (schema v$DATA_SCHEMA_VERSION) — skipping data + SOP generation."
    log "  mode=${BASELINE_MODE:-?}  frozen_at=${BASELINE_FROZEN_AT:-?}  sops=${BASELINE_SOP_COUNT:-?}"
    log "  (use --force / build.sh --regenerate-data to regenerate and re-freeze)"
    exit 0
  fi
fi

[[ "$FORCE" == "1" ]] && log "--force: regenerating and re-freezing." \
                      || log "No frozen baseline yet — generating grounded data + SOP corpus (one time)."

# ---------- 1) deterministic numbers (never AI) ----------
log "1/3 · deterministic dataset (accounts / ledgers / facilities / CRM) ..."
"$PYTHON" "$GENERATE_PY"

# ---------- 2) decide AI vs templated for the NARRATIVE text ----------
MODE="ai"
if [[ "$OFFLINE" == "1" ]]; then
  MODE="offline"
  warn "--offline: freezing a TEMPLATED baseline (no Azure OpenAI)."
else
  if [[ -z "${FOUNDRY_AOAI_ENDPOINT:-}" ]]; then
    die "First-run baseline needs Azure OpenAI, but FOUNDRY_AOAI_ENDPOINT is not set.
       • In a full build this runs on the VM AFTER phase2 (gpt-5.4) via tools/run-generation-on-vm.sh.
       • To freeze a templated (non-AI) baseline instead: re-run with --offline."
  fi
  export FOUNDRY_AOAI_ENDPOINT
  export FOUNDRY_CHAT_DEPLOYMENT="${FOUNDRY_CHAT_DEPLOYMENT:-gpt-5-4}"
  ok "Azure OpenAI ready — host=${FOUNDRY_AOAI_ENDPOINT#https://} deployment=${FOUNDRY_CHAT_DEPLOYMENT}"
fi

# ---------- 3) narrative enrichment + SOP corpus ----------
if [[ "$MODE" == "ai" ]]; then
  log "2/3 · gpt-5.4-enriching dataset narratives ..."
  "$PYTHON" "$ENRICH_PY"
  log "3/3 · gpt-5.4-authoring the SOP corpus (docs/sop/contosobank_*.md) ..."
  "$PYTHON" "$SOP_PY"
else
  log "2/3 · templated dataset narratives ..."
  "$PYTHON" "$ENRICH_PY" --offline
  log "3/3 · templated SOP corpus ..."
  "$PYTHON" "$SOP_PY" --offline
fi

# ---------- Write the freeze sentinel ----------
SOP_COUNT="$( { ls "$SOP_DIR"/contosobank_*.md 2>/dev/null || true; } | wc -l | tr -d ' ')"
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
FOUNDRY_HOST="${FOUNDRY_AOAI_ENDPOINT:-}"; FOUNDRY_HOST="${FOUNDRY_HOST#https://}"; FOUNDRY_HOST="${FOUNDRY_HOST%%/*}"

cat > "$SENTINEL" <<EOF
# Contoso Bank — frozen data + SOP baseline (CONTOSOBANK-SYN v1.0).
# Written by tools/ensure-baseline.sh. COMMIT this file: its presence makes every future build
# SKIP data/SOP generation (the guard checks for this sentinel). Regenerate with
# 'build.sh --regenerate-data' (which forces --force here).
BASELINE_FROZEN_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
BASELINE_SCHEMA_VERSION="$DATA_SCHEMA_VERSION"
BASELINE_MODE="$MODE"
BASELINE_CHAT_DEPLOYMENT="${FOUNDRY_CHAT_DEPLOYMENT:-n/a}"
BASELINE_FOUNDRY_HOST="${FOUNDRY_HOST:-n/a}"
BASELINE_GIT_SHA="$GIT_SHA"
BASELINE_SOP_COUNT="$SOP_COUNT"
EOF
ok "Wrote freeze sentinel: $SENTINEL (mode=$MODE, sops=$SOP_COUNT)"

[[ "$MODE" == "ai" ]] && MODE_LABEL="gpt-5.4-authored (${FOUNDRY_CHAT_DEPLOYMENT:-?})" || MODE_LABEL="templated (offline)"
log "Baseline ready — mode=$MODE_LABEL. The local orchestrator (build.sh) commits + pushes these artifacts."
