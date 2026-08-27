#!/usr/bin/env bash
# wipe.sh — teardown for the Contoso Retail "RM Assist — Rakesh Sharma" demo.
#
# DEFAULT = FULL PURGE. Deletes the ENTIRE billable resource group ($AZ_RG) — the VM and all
# three applications, AI Foundry + both model deployments, AI Search, ACS, Speech, Log
# Analytics and the UAMI — then purges the soft-deleted Cognitive Services accounts so the
# names free up immediately.
#
# NEVER TOUCHED, by design:
#   * $AZ_RG_PERSISTENT — the persistent RG holding the STATIC PUBLIC IP. That IP is what
#     fixes the hostname rmassist.<ip>.nip.io, which is what makes the committed TLS
#     certificate reusable. Delete it and the next build must mint a new certificate.
#   * infra/cert/ — the committed (encrypted) certificate store. It lives in git, not Azure,
#     so no Azure teardown can reach it.
#
# The old behaviour — keep the resource group and the Phase-1 platform so the next build.sh
# is faster — is still available behind --keep-rg.
#
# Thin wrapper around infra/wipe-parallel.sh. Options (env vars):
#   KEEP_PLATFORM=1            with --keep-rg, also preserve the Phase-1 platform (UAMI + LAW)
#   WIPE_RG_NOWAIT=1           submit the RG delete async and return (skips soft-delete purges)
#   WIPE_PURGE_SOFT_DELETED=0  skip purging soft-deleted CogSvc accounts
#   WIPE_GRAPH_APP=0           keep the setup-graph.sh Entra app registration (default: delete it)
#   WIPE_LOCAL_STATE=0         keep generated local state (secrets.env / phase*/outputs.env)
#   WIPE_FORCE=1               delete the RG even if it lacks this project's tag (safety override)
#   ACS_FORCE_DELETE=0         preserve ACS across the wipe
set -eo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

usage() {
  cat <<'USAGE'
Usage: bash wipe.sh [--keep-rg]

  (default)   FULL PURGE. Deletes the ENTIRE billable resource group — the VM, all three
              applications, Foundry + model deployments, AI Search, ACS, Speech, Log
              Analytics and the UAMI — and purges soft-deleted Cognitive Services accounts
              so their names free up immediately.

              NEVER touched: the persistent resource group (static public IP) and the
              committed TLS certificate in infra/cert/. That pair is exactly what lets the
              next build reuse the same hostname and the same certificate with no ACME call.

  --keep-rg   Old behaviour. Tear the billable stack down phase by phase but KEEP the
              resource group and the Phase-1 platform, so the next build.sh is faster.
              Add KEEP_PLATFORM=0 to remove the platform as well.
USAGE
}

# ---- CLI args -------------------------------------------------------------------------
# The default is now the FULL purge; --keep-rg opts back into the per-phase teardown.
CLI_KEEP_RG=""
for arg in "$@"; do
  case "$arg" in
    --keep-rg)    CLI_KEEP_RG="1" ;;
    # --delete-rg used to be how you ASKED for the full purge. It is the default now, so
    # accept it silently rather than failing a muscle-memory invocation.
    --delete-rg)  echo "note: --delete-rg is now the DEFAULT — no need to pass it." >&2 ;;
    --type=*|--type) echo "note: --type is obsolete (chat model is always gpt-5.4 GlobalStandard) — ignoring." >&2 ;;
    -h|--help)    usage; exit 0 ;;
    *)            echo "Unknown argument: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -n "$CLI_KEEP_RG" ]]; then
  export WIPE_DELETE_RG=0
  # When keeping the RG, also keep the Phase-1 platform by default (fast re-build.sh).
  export KEEP_PLATFORM="${KEEP_PLATFORM:-1}"
else
  export WIPE_DELETE_RG=1
  export KEEP_PLATFORM=0
fi

find infra videoassist -type f -name '*.sh' -exec chmod +x {} + 2>/dev/null || true
exec bash infra/wipe-parallel.sh
