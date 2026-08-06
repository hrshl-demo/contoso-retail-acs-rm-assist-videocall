#!/usr/bin/env bash
# infra/rebuild-parallel.sh
#
# Parallel rebuild. Same phases as rebuild-all.sh but runs independent phases
# concurrently to cut wall-clock time. Each up.sh is idempotent.
#
# Real dependency graph (from each up.sh's inputs):
#   phase0  -> (none)
#   phase1  -> phase0
#   phase2  -> phase1            (creates Foundry acct+project, model deployments, Search, ACS)
#   phase3  -> (none, standalone synthetic data)
#   phase4  -> phase1
#   phase5  -> phase1, phase2, phase4
#   phase6  -> phase1, phase4, phase9   (injects the Video Assist URL into Step 7)
#   phase9  -> phase1, phase4           (Video Assist; grounds on phase4 Tool API)
#
# Build waves (within a wave, phases are independent and run in parallel):
#   Wave 0:  phase0-foundation
#   Wave 1:  phase1-platform           (then docker cache setup)
#   Wave 2:  phase2-ai  ∥  phase3-data   (phase2 creates Foundry + deployments + Search + ACS)
#   Wave 3:  phase4-toolapi            (reads phase2's ACS/speech config via outputs.env)
#   Wave 4:  phase5-rag ∥  phase9-videoassist
#   Wave 5:  phase6-crm                (last: injects VIDEOASSIST_URL from phase9)
#
# A failed wave aborts the rebuild (a dependency is missing for later waves).
set -uo pipefail
PHASE="rebuild"; export PHASE
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common/env.sh
source "$SCRIPT_DIR/common/env.sh"
# shellcheck source=common/run_wave.sh
source "$SCRIPT_DIR/common/run_wave.sh"

# BUILD_STAGE selects which portion of the pipeline to run (see env.sh §8c):
#   all (default, deploy.sh) | foundation (build_rg.sh) | apps (build.sh)
BUILD_STAGE="${BUILD_STAGE:-all}"
case "$BUILD_STAGE" in
  all|foundation|apps) ;;
  *) warn "Unknown BUILD_STAGE '$BUILD_STAGE' — falling back to 'all'."; BUILD_STAGE="all" ;;
esac
export BUILD_STAGE

cat <<EOF

$(printf '\033[1;36m╔════════════════════════════════════════════════════════════╗\033[0m')
$(printf '\033[1;36m║   CONTOSO RETAIL · RM ASSIST (Rakesh Sharma) · REBUILD      ║\033[0m')
$(printf '\033[1;36m╚════════════════════════════════════════════════════════════╝\033[0m')

RG $AZ_RG · region $AZ_REGION · tag $PROJECT_TAG   [RG CREATED by this build]
Stage: $BUILD_STAGE  ($([ "$BUILD_STAGE" == "foundation" ] && echo "phase0+phase1 only — non-billable foundation" || { [ "$BUILD_STAGE" == "apps" ] && echo "phase2..phase9 — billable, reuses the existing foundation" || echo "all phases — one-shot"; }))
Creates AI Foundry: $NAME_AISERVICES / $NAME_FOUNDRY_PROJECT
Chat model: $AOAI_CHAT_DEPLOYMENT_NAME ($AOAI_CHAT_MODEL_NAME, $AOAI_CHAT_SKU_NAME) in $AZ_REGION_AOAI — created by phase2, deleted with the RG on wipe
Embed model: $AOAI_EMBED_DEPLOYMENT_NAME ($AOAI_EMBED_SKU_NAME) — created by phase2, deleted with the RG on wipe
Data-gen VM: $([ "${SKIP_VMHOST:-0}" == "1" ] && echo "SKIPPED (SKIP_VMHOST=1)" || echo "$NAME_VM + Caddy/TLS at $(rmassist_host 2>/dev/null || echo 'rmassist.<ip>.nip.io') — keyless gpt-5.4 generation")
Logs: ${ACS_BUILD_LOGDIR:-/tmp/acs_build_logs}/<phase>.up.log
EOF

# Fail fast before Azure login or any resource creation. This validation gate is
# deliberately non-interactive and uses only local data/tests.
PREFLIGHT="$SCRIPT_DIR/common/preflight_validate.sh"
if [[ ! -f "$PREFLIGHT" ]]; then
  warn "Missing preflight validator: $PREFLIGHT"
  exit 1
fi
if ! bash "$PREFLIGHT"; then
  warn "Preflight validation failed. Azure rebuild was not started."
  exit 1
fi

log "Preflight passed. Starting Azure rebuild without a confirmation prompt."
ensure_az_login

abort() { warn "Rebuild aborted at $1. See ${ACS_BUILD_LOGDIR:-/tmp/acs_build_logs}/$1.up.log"; exit 1; }

T_START=$(date +%s)

# ======================================================================================
# FOUNDATION (phase0 + phase1) — non-billable substrate. Runs for BUILD_STAGE=all and
# BUILD_STAGE=foundation (build_rg.sh). Skipped for BUILD_STAGE=apps (build.sh reuses it).
# ======================================================================================
if [[ "$BUILD_STAGE" != "apps" ]]; then
  ensure_rg
  run_wave up phase0-foundation || abort "phase0-foundation"
  run_wave up phase1-platform   || abort "phase1-platform"

  if [[ "$BUILD_STAGE" == "foundation" ]]; then
    TOTAL=$(( $(date +%s) - T_START ))
    cat <<EOF

$(printf '\033[1;32m╔════════════════════════════════════════════════════════════╗\033[0m')
$(printf '\033[1;32m║   Foundation ready (RG + platform) in %02d:%02d                   ║\033[0m' "$(( TOTAL/60 ))" "$(( TOTAL%60 ))")
$(printf '\033[1;32m╚════════════════════════════════════════════════════════════╝\033[0m')

Created (kept across demos — ACR Basic ~\$5/mo is the only standing cost):
  Log Analytics · ACR · UAMI · Container Apps environment  in RG $AZ_RG
Next: bash build.sh            (PTU, billable)
  or: bash build.sh --type=payg (pay-as-you-go, billable)
EOF
    ok "Foundation stage complete."
    exit 0
  fi
else
  # BUILD_STAGE=apps: normally REUSE the foundation that build_rg.sh created. But if the
  # foundation is missing/incomplete — e.g. after 'wipe.sh --delete-rg', a fresh tarball
  # extract, or a partial teardown — SELF-HEAL by building it now (phase0 + phase1) rather
  # than failing with "run build_rg.sh first". This makes 'bash build.sh' a one-command
  # deploy from any state. Set AUTO_FOUNDATION=0 to restore the old hard stop.
  if foundation_present; then
    ok "Foundation present: RG + ACR + UAMI + Container Apps env."
    regen_phase1_outputs
  elif [[ "${AUTO_FOUNDATION:-1}" == "1" ]]; then
    warn "Foundation missing/incomplete: ${FOUNDATION_MISSING[*]}"
    log  "Auto-building the non-billable foundation now (phase0 + phase1). Disable with AUTO_FOUNDATION=0."
    ensure_rg
    run_wave up phase0-foundation || abort "phase0-foundation"
    run_wave up phase1-platform   || abort "phase1-platform"
    assert_foundation_present
  else
    assert_foundation_present   # AUTO_FOUNDATION=0 -> original hard stop with guidance
  fi
fi

# ======================================================================================
# APPS (phase2 .. phase9) — the billable stack. Runs for BUILD_STAGE=all and =apps.
# ======================================================================================
# Docker Hub cache rule on ACR (needs phase1). Idempotent; no-ops without creds.
CACHE_SETUP="$SCRIPT_DIR/common/setup_docker_cache.sh"
[[ -x "$CACHE_SETUP" ]] && { bash "$CACHE_SETUP" || warn "Docker cache setup failed (frontends may hit rate limit)"; }

# SPEED: pre-build all three container images CONCURRENTLY in the background so they run
# while phase2 provisions AI Search (the slow long pole). phase4/6/9 then reuse the ready
# images instead of building in series. Falls back to inline builds if a build fails.
PREBUILD_LOG="${ACS_BUILD_LOGDIR:-/tmp/acs_build_logs}/prebuild-images.log"
PREBUILD_PID=""
rm -f "$SCRIPT_DIR/common/prebuilt_images.env"
if [[ "${PREBUILD_IMAGES:-1}" == "1" ]]; then
  log "Pre-building container images in parallel (overlaps phase2). Log: $PREBUILD_LOG"
  ( bash "$SCRIPT_DIR/common/prebuild_images.sh" ) >"$PREBUILD_LOG" 2>&1 &
  PREBUILD_PID=$!
fi

# phase2 (create Foundry acct+project+model deployments + Search + ACS + role grant +
# monitoring) ∥ phase3 (data).
# NOTE: phase4 reads phase2's outputs.env (ACS endpoint/connection-string, speech region,
# Foundry/Search endpoints), so phase4 MUST run AFTER phase2 — not in the same wave.
run_wave up phase2-ai phase3-data || abort "wave2(phase2/phase3)"

# Ensure the parallel image pre-build has finished before phases that consume the images.
if [[ -n "$PREBUILD_PID" ]]; then
  log "Waiting for parallel image pre-build to finish (usually already done under phase2)..."
  if wait "$PREBUILD_PID"; then
    ok "Parallel image pre-build complete."
  else
    warn "Image pre-build reported issues; phases will build any missing images inline. See $PREBUILD_LOG"
  fi
fi

# ---- Persistent layer + data-gen VM (phase10) + keyless gpt-5.4 generation on the VM --------
# The VM (billable, in $AZ_RG) hosts Caddy/TLS and runs the dataset + SOP generation keylessly
# via its managed identity. Its public IP is the PERSISTENT static IP (in the never-wiped
# persistent RG) that anchors the reusable Let's Encrypt cert. Set SKIP_VMHOST=1 to skip the VM
# entirely (no data-gen, no cert), or SKIP_DATAGEN=1 to bring up the VM but not (re)generate.
if [[ "${SKIP_VMHOST:-0}" != "1" ]]; then
  # Self-heal the persistent layer so 'bash build.sh' works from any state (idempotent).
  if [[ -z "$(persist_ip 2>/dev/null)" ]]; then
    warn "Persistent static IP absent — bootstrapping the persistent layer once (build_persistent.sh)."
    bash "$SCRIPT_DIR/../build_persistent.sh" || abort "build_persistent"
  else
    ok "Persistent static IP present: $(persist_ip) ($(rmassist_host))"
  fi
  # phase4 (Tool API) and phase10 (VM/Caddy/cert) are independent given phase2 — build in parallel.
  run_wave up phase4-toolapi phase10-vmhost || abort "wave3(phase4/phase10)"

  # Generate/refresh the Contoso Bank dataset + SOP corpus ON THE VM (keyless gpt-5.4). The
  # BASELINE_FROZEN sentinel makes this a fast no-op unless REGENERATE_DATA=1 forces a rebuild.
  if [[ "${SKIP_DATAGEN:-0}" != "1" ]]; then
    log "Generating/refreshing Contoso Bank data + SOPs on the VM (keyless gpt-5.4)$([ "${REGENERATE_DATA:-0}" == "1" ] && echo ' — REGENERATE forced')..."
    if [[ "${REGENERATE_DATA:-0}" == "1" ]]; then
      bash "$SCRIPT_DIR/../tools/run-generation-on-vm.sh" --regenerate-data || abort "run-generation-on-vm"
    else
      bash "$SCRIPT_DIR/../tools/run-generation-on-vm.sh" || abort "run-generation-on-vm"
    fi
  else
    warn "SKIP_DATAGEN=1 — VM is up but data/SOP generation was skipped."
  fi
else
  warn "SKIP_VMHOST=1 — skipping the data-gen/Caddy VM (phase10), the persistent layer and data generation."
  # phase4 still needs to come up for the app pipeline.
  run_wave up phase4-toolapi || abort "phase4-toolapi"
fi

# Wave 4 leaves: all need phase4; phase5 also needs phase2; phase9 grounds on phase4's Tool API.
run_wave up phase5-rag phase9-videoassist || abort "wave4(phase5/phase9)"

# Wave 5: CRM dashboard last — it injects the Video Assist URL (phase9) so Step 7
# (the capstone) launches the video call pre-bound to Rakesh Sharma (CTB-RTL-002).
run_wave up phase6-crm || abort "phase6-crm"

TOTAL=$(( $(date +%s) - T_START ))
cat <<EOF

$(printf '\033[1;32m╔════════════════════════════════════════════════════════════╗\033[0m')
$(printf '\033[1;32m║   Parallel rebuild complete in %02d:%02d                         ║\033[0m' "$(( TOTAL/60 ))" "$(( TOTAL%60 ))")
$(printf '\033[1;32m╚════════════════════════════════════════════════════════════╝\033[0m')
EOF
print_demo_urls 2>/dev/null || true
ok "Demo-ready."
