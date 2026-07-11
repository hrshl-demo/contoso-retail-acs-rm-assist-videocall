#!/usr/bin/env bash
# infra/common/run_wave.sh
#
# run_wave <direction> <phase_dir> [phase_dir ...]
#   direction: "up" or "down"
# Runs the given phases CONCURRENTLY (background jobs), one log file each under
# /tmp/acs_build_logs, streams a compact live status, and waits for all. Returns
# non-zero if ANY phase in the wave failed (for "up"; "down" is best-effort).
#
# This is sourced by the parallel orchestrators. It deliberately runs only phases
# that are independent of each other (same dependency wave) — the orchestrator is
# responsible for wave ordering.

LOGDIR="${ACS_BUILD_LOGDIR:-/tmp/acs_build_logs}"
mkdir -p "$LOGDIR"

run_wave() {
  local direction="$1"; shift
  local phases=("$@")
  local script_name pids=() names=() starts=() rc_any=0
  [[ "$direction" == "up" ]] && script_name="up.sh" || script_name="down.sh"

  local INFRA_DIR
  INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

  echo ""
  printf '\033[1;36m▶ WAVE [%s]: %s  (parallel)\033[0m\n' "$direction" "${phases[*]}"
  printf '\033[1;36m  logs: %s/<phase>.%s.log\033[0m\n' "$LOGDIR" "$direction"

  local p
  for p in "${phases[@]}"; do
    local s="$INFRA_DIR/$p/$script_name"
    if [[ ! -f "$s" ]]; then
      printf '\033[1;33m  skip %s (no %s)\033[0m\n' "$p" "$script_name"
      continue
    fi
    local logf="$LOGDIR/${p}.${direction}.log"
    # auto-answer prompts (DELETE/REBUILD/y) from inside each phase script
    ( printf 'y\nDELETE\nREBUILD\ny\n' | bash "$s" >"$logf" 2>&1 ) &
    pids+=("$!")
    names+=("$p")
    starts+=("$(date +%s)")
    printf '  started %-18s pid=%s\n' "$p" "$!"
  done

  # Wait for all, collect rc, and print a heartbeat for long-running phases.
  # Previously the parent terminal stayed silent while a child waited on Azure
  # Resource Manager, which made a live deployment look frozen.
  local i heartbeat_seconds="${ACS_BUILD_HEARTBEAT_SECONDS:-45}"
  [[ "$heartbeat_seconds" =~ ^[1-9][0-9]*$ ]] || heartbeat_seconds=45

  for i in "${!pids[@]}"; do
    local pid="${pids[$i]}"
    local name="${names[$i]}"
    local logf="$LOGDIR/${names[$i]}.${direction}.log"

    local last_heartbeat=0
    while true; do
      local process_state elapsed
      process_state="$(ps -o stat= -p "$pid" 2>/dev/null | awk '{print $1}')"
      [[ -n "$process_state" && "$process_state" != Z* ]] || break

      sleep 2
      elapsed=$(( $(date +%s) - starts[$i] ))

      if (( elapsed - last_heartbeat >= heartbeat_seconds )); then
        process_state="$(ps -o stat= -p "$pid" 2>/dev/null | awk '{print $1}')"
        if [[ -n "$process_state" && "$process_state" != Z* ]]; then
          local last_line suffix
          last_line="$(tail -n 1 "$logf" 2>/dev/null | tr '\r\n' ' ' | cut -c1-180)"
          suffix=""
          [[ -n "$last_line" ]] && suffix=" · $last_line"
          printf '\033[1;36m  … %-18s running %02d:%02d\033[0m%s\n' \
            "$name" "$((elapsed/60))" "$((elapsed%60))" "$suffix"
          last_heartbeat="$elapsed"
        fi
      fi
    done

    if wait "$pid"; then
      printf '\033[1;32m  ✓ %-18s ok\033[0m   (%s)\n' "$name" "$logf"
    else
      rc_any=1
      printf '\033[1;31m  ✗ %-18s FAILED\033[0m (%s)\n' "$name" "$logf"
      tail -n 25 "$logf" 2>/dev/null | sed 's/^/      /' || true
    fi
  done
  return $rc_any
}
