#!/usr/bin/env bash
# wipe.sh — teardown for the Contoso Retail "RM Assist — Rakesh Sharma" demo.
#
# DEFAULT (3-script model): tears down the BILLABLE stack that build.sh created
# (AI Foundry account + project, both model deployments, AI Search, ACS + Email, Speech,
# and the container apps) but KEEPS the resource group AND the Phase-1 platform foundation
# (Log Analytics, ACR, UAMI, Container Apps environment) that build_rg.sh
# created. This means you can re-run build.sh for the next demo without re-running
# build_rg.sh.
#
# FULL teardown: pass --delete-rg (or WIPE_DELETE_RG=1) to DELETE THE ENTIRE RESOURCE
# GROUP — foundation included — and purge the soft-deleted Cognitive Services accounts
# so the names free up.
#
# Thin wrapper around infra/wipe-parallel.sh. Options (env vars):
#   WIPE_DELETE_RG=1        FULL teardown: delete the whole resource group (same as --delete-rg)
#   KEEP_PLATFORM=0         also tear down the Phase-1 platform (only meaningful when keeping the RG)
#   WIPE_RG_NOWAIT=1        submit the RG delete async and return (skips soft-delete purges)
#   WIPE_PURGE_SOFT_DELETED=0  skip purging soft-deleted CogSvc accounts
#   WIPE_GRAPH_APP=0        keep the setup-graph.sh Entra app registration (default: delete it)
#   WIPE_LOCAL_STATE=0      keep generated local state (secrets.env / phase*/outputs.env)
#   WIPE_FORCE=1           delete the RG even if it lacks this project's tag (safety override)
#   ACS_FORCE_DELETE=0     preserve ACS across the wipe
set -eo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

usage() {
  cat <<'USAGE'
Usage: bash wipe.sh [--delete-rg]
  (default)     tear down the billable stack; KEEP the resource group + Phase-1 platform.
  --delete-rg   FULL PURGE: delete the entire resource group (foundation + VM included), purge
                soft-deleted Cognitive Services accounts, and verify no residue. The persistent
                RG (static IP + committed cert) is NEVER touched.
USAGE
}

# ---- CLI args -------------------------------------------------------------------------
# --delete-rg switches to the FULL all-or-nothing purge (delete the whole RG + verify).
# (--type=ptu|payg is accepted-but-ignored for backward compatibility with the old 3-script model.)
CLI_DELETE_RG=""
for arg in "$@"; do
  case "$arg" in
    --delete-rg)  CLI_DELETE_RG="1" ;;
    --type=*|--type) echo "note: --type is obsolete (chat model is always gpt-5.4 GlobalStandard) — ignoring." >&2 ;;
    -h|--help)    usage; exit 0 ;;
    *)            echo "Unknown argument: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

# New default = keep the RG + Phase-1 platform (the foundation build_rg.sh created).
# --delete-rg (or an explicit WIPE_DELETE_RG=1 in the environment) forces the full wipe.
if [[ -n "$CLI_DELETE_RG" ]]; then
  export WIPE_DELETE_RG=1
else
  export WIPE_DELETE_RG="${WIPE_DELETE_RG:-0}"
fi
# When keeping the RG, also keep the Phase-1 platform by default (fast re-build.sh).
export KEEP_PLATFORM="${KEEP_PLATFORM:-1}"

find infra videoassist -type f -name '*.sh' -exec chmod +x {} + 2>/dev/null || true
exec bash infra/wipe-parallel.sh
