#!/usr/bin/env bash
# infra/common/preflight_validate.sh
#
# Zero-cost, fail-fast validation gate. Runs BEFORE any Azure login or resource
# creation. Self-contained (no external test suite): validates the synthetic data,
# compiles the backend, and syntax-checks the JavaScript and shell scripts.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT_DIR"

C_RESET='\033[0m'; C_CYAN='\033[1;36m'; C_GREEN='\033[1;32m'; C_RED='\033[1;31m'; C_YELLOW='\033[1;33m'
pass_count=0

run_check() {
  local label="$1"; shift
  printf "\n${C_CYAN}[preflight]${C_RESET} %s\n" "$label"
  if "$@"; then
    pass_count=$((pass_count + 1))
    printf "${C_GREEN}[passed]${C_RESET} %s\n" "$label"
  else
    local rc=$?
    printf "\n${C_RED}[failed]${C_RESET} %s (exit %s)\n" "$label" "$rc" >&2
    printf "${C_YELLOW}Azure rebuild has NOT started. Fix this failure and rerun infra/rebuild-parallel.sh.${C_RESET}\n" >&2
    exit "$rc"
  fi
}

cat <<EOF_BANNER

$(printf '\033[1;36m╔════════════════════════════════════════════════════════════╗\033[0m')
$(printf '\033[1;36m║   PRE-DEPLOYMENT VALIDATION · NO AZURE RESOURCES CREATED    ║\033[0m')
$(printf '\033[1;36m╚════════════════════════════════════════════════════════════╝\033[0m')

The rebuild will start only if every validation below passes.
EOF_BANNER

command -v python >/dev/null 2>&1 || {
  printf "${C_RED}[failed]${C_RESET} python is not installed or not on PATH.\n" >&2
  exit 127
}

# ---- 1) Synthetic data: generate + validate (deterministic; writes to data/) ----
run_check "Generate and validate deterministic datasets" \
  bash infra/phase3-data/up.sh

# ---- 2) Backend compiles ----
check_backend_compile() {
  local f rc=0
  while IFS= read -r -d '' f; do
    python -m py_compile "$f" || { echo "py_compile failed: $f" >&2; rc=1; }
  done < <(find backend/app -name '*.py' -print0)
  return $rc
}
run_check "Backend Python compiles (backend/app/**.py)" check_backend_compile

# ---- 3) JavaScript syntax (only if node is available; otherwise skip) ----
check_js_syntax() {
  if ! command -v node >/dev/null 2>&1; then
    printf "${C_YELLOW}[skip]${C_RESET} node not found — skipping JS syntax check (built in-container at deploy).\n"
    return 0
  fi
  local f rc=0
  for f in frontend-crm/html/app.js frontend-crm/html/ui.js \
           videoassist/server.js videoassist/nudge-engine.js \
           videoassist/teams.js videoassist/toolapi.js \
           videoassist/client/main.js videoassist/public/schedule.js; do
    [[ -f "$f" ]] || continue
    node --check "$f" || { echo "node --check failed: $f" >&2; rc=1; }
  done
  return $rc
}
run_check "Frontend / Video Assist JavaScript syntax" check_js_syntax

# ---- 4) Shell syntax for every infra + videoassist script ----
check_shell_syntax() {
  local f rc=0
  while IFS= read -r -d '' f; do
    bash -n "$f" || { echo "bash -n failed: $f" >&2; rc=1; }
  done < <(find infra videoassist -name '*.sh' -print0)
  return $rc
}
run_check "Infrastructure + Video Assist shell syntax" check_shell_syntax

printf "\n${C_GREEN}╔════════════════════════════════════════════════════════════╗${C_RESET}\n"
printf "${C_GREEN}║   PREFLIGHT PASSED · %2d/%2d CHECKS                            ║${C_RESET}\n" "$pass_count" "$pass_count"
printf "${C_GREEN}╚════════════════════════════════════════════════════════════╝${C_RESET}\n"
printf "All local validations passed. Azure rebuild may now begin.\n\n"
