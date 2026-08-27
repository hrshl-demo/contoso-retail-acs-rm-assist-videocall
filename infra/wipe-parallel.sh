#!/usr/bin/env bash
# infra/wipe-parallel.sh
#
# ALL-OR-NOTHING teardown. This build is self-contained: it CREATED its own resource
# group and everything inside it, so the canonical wipe simply DELETES THE WHOLE
# RESOURCE GROUP and then purges the soft-deleted resources whose names would otherwise
# block a clean redeploy (Cognitive Services / AI Foundry accounts).
#
#   Default (WIPE_DELETE_RG=1):
#     1. Remove the stray Entra app registration setup-graph.sh created (WIPE_GRAPH_APP=1).
#     2. Safety-check the RG carries our project tag (skip with WIPE_FORCE=1).
#     3. az group delete  -> removes Foundry acct+project, BOTH model deployments,
#        AI Search, ACS, Speech, Container Apps, ACR, UAMI, LAW — everything.
#     4. Purge soft-deleted Cognitive Services accounts ($NAME_AISERVICES, $NAME_SPEECH)
#        so the names are immediately reusable.
#     5. Clear stale local state — secrets.env / phase*/outputs.env (WIPE_LOCAL_STATE=1).
#
#   Optional graceful per-phase teardown (WIPE_DELETE_RG=0): tears resources down phase by
#   phase in parallel waves but LEAVES the (now-empty) resource group in place. Useful for
#   fast iteration when you want to keep the RG shell.
#
# Extra knobs: WIPE_GRAPH_APP=0 keeps the Entra app registration; WIPE_LOCAL_STATE=0 keeps
# the generated secrets.env / outputs.env files.
#
# Teardown is best-effort: failures do not abort (we still want to remove as much as possible).
set -uo pipefail
PHASE="wipe"; export PHASE
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common/env.sh
source "$SCRIPT_DIR/common/env.sh"
# shellcheck source=common/run_wave.sh
source "$SCRIPT_DIR/common/run_wave.sh"

# ---- Knobs (all overridable; safe defaults = full all-or-nothing wipe) ----------------
WIPE_DELETE_RG="${WIPE_DELETE_RG:-1}"                 # 1 = delete the whole RG (canonical)
WIPE_RG_NOWAIT="${WIPE_RG_NOWAIT:-0}"                 # 1 = return without waiting (skips purge)
WIPE_PURGE_SOFT_DELETED="${WIPE_PURGE_SOFT_DELETED:-1}"  # purge soft-deleted CogSvc
WIPE_FORCE="${WIPE_FORCE:-0}"                         # 1 = skip the project-tag safety check
WIPE_GRAPH_APP="${WIPE_GRAPH_APP:-1}"                 # 1 = delete the setup-graph.sh Entra app registration
WIPE_LOCAL_STATE="${WIPE_LOCAL_STATE:-1}"            # 1 = remove generated local state (secrets.env/outputs.env)
export WIPE_DELETE_RG WIPE_RG_NOWAIT WIPE_PURGE_SOFT_DELETED WIPE_FORCE WIPE_GRAPH_APP WIPE_LOCAL_STATE

# wipe_graph_app_registration — delete the Entra app registration that setup-graph.sh
# created for the calendared Teams-meeting flow. It is a DIRECTORY object, not a resource
# in the RG, so 'az group delete' never removes it — without this it lingers as a stray.
# Best-effort: needs app-owner or an admin directory role; failures do not abort the wipe.
wipe_graph_app_registration() {
  [[ "${WIPE_GRAPH_APP:-1}" == "1" ]] || { log "WIPE_GRAPH_APP=0 — preserving the Entra app registration."; return 0; }
  local name="${GRAPH_APP_NAME:-contoso-videoassist-rm-calendar}"
  local ids id
  ids="$(az ad app list --display-name "$name" --query "[].id" -o tsv 2>/dev/null || true)"
  if [[ -z "$ids" ]]; then
    log "No Entra app registration named '$name' — nothing to remove."
    return 0
  fi
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    if az ad app delete --id "$id" 2>/dev/null; then
      ok "Deleted orphan Entra app registration '$name' ($id)."
    else
      warn "Could not delete Entra app registration '$name' ($id) — need app-owner or admin rights. Continuing."
    fi
    # Best-effort permanent purge from deleted items so it does not linger for 30 days.
    az rest --method DELETE --url "https://graph.microsoft.com/v1.0/directory/deletedItems/$id" -o none 2>/dev/null \
      && ok "Purged soft-deleted app '$name' ($id)." || true
  done <<< "$ids"
}

# wipe_local_state — remove generated files that reference now-deleted Azure/Entra
# resources, so a later 'source env.sh' / build starts clean. secrets.env holds the deleted
# Graph app's client secret (removed only when we actually deleted that app); each phase's
# outputs.env points at deleted resources (regenerable from Azure by env.sh anyway).
wipe_local_state() {
  [[ "${WIPE_LOCAL_STATE:-1}" == "1" ]] || return 0
  local root="$SCRIPT_DIR" f base
  if [[ "${WIPE_GRAPH_APP:-1}" == "1" ]]; then
    rm -f "$root/common/secrets.env" 2>/dev/null && ok "Removed infra/common/secrets.env (stale Graph creds)." || true
  fi
  rm -f "$root/common/prebuilt_images.env" 2>/dev/null || true
  for f in "$root"/phase*/outputs.env; do
    [[ -e "$f" ]] || continue
    base="$(basename "$(dirname "$f")")"
    # If the Phase-1 platform is intentionally kept, preserve its outputs.env.
    if [[ "${KEEP_PLATFORM:-0}" == "1" && ( "$base" == "phase0-foundation" || "$base" == "phase1-platform" ) ]]; then
      continue
    fi
    rm -f "$f" 2>/dev/null && log "Removed $base/outputs.env" || true
  done
}

cat <<EOF

$(printf '\033[1;31m╔════════════════════════════════════════════════════════════╗\033[0m')
$(printf '\033[1;31m║   CONTOSO RETAIL · RM ASSIST (Rakesh Sharma) · WIPE         ║\033[0m')
$(printf '\033[1;31m╚════════════════════════════════════════════════════════════╝\033[0m')

Mode: $([ "$WIPE_DELETE_RG" == "1" ] && echo "FULL — DELETE the entire resource group $AZ_RG (foundation included)" || echo "per-phase teardown — KEEP the resource group $AZ_RG$([ "${KEEP_PLATFORM:-0}" == "1" ] && echo " + Phase-1 platform")")
Deletes the billable stack this demo created:
  • AI Foundry account + project      $NAME_AISERVICES / $NAME_FOUNDRY_PROJECT
  • Chat deployment                   $AOAI_CHAT_DEPLOYMENT_NAME ($AOAI_CHAT_SKU_NAME)
  • Embedding deployment              $AOAI_EMBED_DEPLOYMENT_NAME ($AOAI_EMBED_SKU_NAME)
  • AI Search / ACS / Speech          $NAME_SEARCH / $NAME_ACS / $NAME_SPEECH
$([ "$WIPE_DELETE_RG" == "1" ] && echo "  • VM host + disk/NIC/NSG/VNet       $NAME_VM (deleted with the RG)")
$([ "$WIPE_DELETE_RG" == "1" ] && echo "  • UAMI / Log Analytics              (Phase 1 platform)" || { [ "${KEEP_PLATFORM:-0}" == "1" ] && echo "  • (KEEPS UAMI / Log Analytics — the Phase 1 platform build_rg.sh created)" || echo "  • UAMI / Log Analytics              (Phase 1 platform)"; })
PRESERVED (never touched by wipe): persistent RG $AZ_RG_PERSISTENT (static IP + committed cert), and all committed git artifacts (data/contosobank, docs/sop, infra/cert).
$([ "$WIPE_DELETE_RG" == "1" ] && [ "$WIPE_PURGE_SOFT_DELETED" == "1" ] && echo "Then purges soft-deleted $NAME_AISERVICES and $NAME_SPEECH so names free up.")
$([ "${WIPE_GRAPH_APP:-1}" == "1" ] && echo "Also removes the stray Entra app registration '${GRAPH_APP_NAME:-contoso-videoassist-rm-calendar}' (setup-graph.sh).")
$([ "${WIPE_LOCAL_STATE:-1}" == "1" ] && echo "Also clears stale local state (infra/common/secrets.env, phase*/outputs.env).")
Logs: ${ACS_BUILD_LOGDIR:-/tmp/acs_build_logs}/<phase>.down.log
EOF

warn "Non-interactive wipe requested. Teardown starts immediately; no DELETE confirmation is required."

ensure_az_login

# Remove the orphan Entra app registration that setup-graph.sh created. It is a directory
# object (not in the RG), so it survives 'az group delete' — clean it up in BOTH wipe modes.
wipe_graph_app_registration

T_START=$(date +%s)

# ======================================================================================
# Canonical path: delete the whole resource group, then purge soft-deleted resources.
# ======================================================================================
if [[ "$WIPE_DELETE_RG" == "1" ]]; then
  if ! az group show --name "$AZ_RG" -o none 2>/dev/null; then
    warn "Resource group '$AZ_RG' not found — nothing to delete."
  else
    # Safety: only delete an RG that carries THIS project's tag (unless forced). Protects a
    # shared/pre-existing RG if AZ_RG is ever pointed at one against the self-contained design.
    RG_TAG_VALUE="$(az group show --name "$AZ_RG" --query "tags.${PROJECT_TAG_KEY}" -o tsv 2>/dev/null || true)"
    if [[ "$WIPE_FORCE" != "1" && "$RG_TAG_VALUE" != "$PROJECT_TAG_VALUE" ]]; then
      warn "RG '$AZ_RG' is not tagged ${PROJECT_TAG_KEY}=${PROJECT_TAG_VALUE} (found: '${RG_TAG_VALUE:-<none>}')."
      die  "Refusing to delete an RG this build did not create. Re-run with WIPE_FORCE=1 to override."
    fi
    if [[ "$WIPE_RG_NOWAIT" == "1" ]]; then
      log "Deleting resource group '$AZ_RG' (async, --no-wait)..."
      az group delete --name "$AZ_RG" --yes --no-wait -o none \
        && ok "RG delete submitted (running in background)." \
        || warn "RG delete submission failed."
      warn "WIPE_RG_NOWAIT=1 — skipping soft-delete purges (the RG delete has not completed yet)."
    else
      log "Deleting resource group '$AZ_RG' (waiting for completion; this is the slow step)..."
      if az group delete --name "$AZ_RG" --yes -o none; then
        ok "Resource group '$AZ_RG' deleted."
      else
        warn "RG delete reported an error — some resources may remain. Check the portal."
      fi

      # Purge soft-deleted resources so their (globally-unique) names are immediately reusable.
      # Soft-delete LAGS the RG delete, so a single purge often "succeeds" without freeing the
      # name — retry each account a few times. The Foundry account lives in $AZ_REGION_AOAI (it
      # may differ from $AZ_REGION so gpt-5.4 GlobalStandard is available); Speech in $AZ_REGION_SPEECH.
      if [[ "$WIPE_PURGE_SOFT_DELETED" == "1" ]]; then
        log "Purging soft-deleted Cognitive Services accounts so names free up (with retries)..."
        for pair in "$NAME_AISERVICES:$AZ_REGION_AOAI" "$NAME_SPEECH:$AZ_REGION_SPEECH"; do
          acct="${pair%%:*}"; acct_region="${pair##*:}"
          [[ -n "$acct" ]] || continue
          purged=0
          for attempt in $(seq 1 "${WIPE_PURGE_ATTEMPTS:-8}"); do
            # Already absent from the soft-deleted list -> name is free, done.
            if ! az cognitiveservices account list-deleted --query "[?name=='$acct'] | [0].name" -o tsv 2>/dev/null | grep -q .; then
              ok "Soft-deleted '$acct' not present (name free)."; purged=1; break
            fi
            if az cognitiveservices account purge \
                 --name "$acct" --resource-group "$AZ_RG" --location "$acct_region" -o none 2>/dev/null; then
              ok "Purged soft-deleted Cognitive Services account: $acct ($acct_region)"; purged=1; break
            fi
            log "  purge attempt $attempt/${WIPE_PURGE_ATTEMPTS:-8} for '$acct' not ready (soft-delete lagging); waiting..."
            sleep "${WIPE_PURGE_SLEEP:-15}"
          done
          [[ "$purged" == "1" ]] || warn "Could not purge '$acct' after ${WIPE_PURGE_ATTEMPTS:-8} attempts — run 'bash tools/az-clean-slate.sh' to finish."
        done
      fi

      # Verification gate: confirm NOTHING survives (no RG, no soft-deleted names). This is what
      # makes the wipe a true full purge rather than a best-effort delete. The persistent RG
      # ($AZ_RG_PERSISTENT — static IP + committed cert) is intentionally NOT checked/deleted.
      if [[ "$WIPE_PURGE_SOFT_DELETED" == "1" ]]; then
        residue=0
        if az group show --name "$AZ_RG" -o none 2>/dev/null; then
          warn "Residue: resource group '$AZ_RG' still present (deletion may still be finalizing)."; residue=1
        fi
        for acct in "$NAME_AISERVICES" "$NAME_SPEECH"; do
          [[ -n "$acct" ]] || continue
          if az cognitiveservices account list-deleted --query "[?name=='$acct'] | [0].name" -o tsv 2>/dev/null | grep -q .; then
            warn "Residue: soft-deleted account still present: $acct"; residue=1
          fi
        done
        if [[ "$residue" == "0" ]]; then
          ok "Full purge verified — no RG and no soft-deleted residue. Persistent RG '$AZ_RG_PERSISTENT' preserved."
        else
          warn "Purge left residue. Run 'bash tools/az-clean-slate.sh' (waits + retries + verifies)."
        fi
      fi
    fi
  fi

  TOTAL=$(( $(date +%s) - T_START ))
  cat <<EOF

$(printf '\033[1;32m╔════════════════════════════════════════════════════════════╗\033[0m')
$(printf '\033[1;32m║   All-or-nothing wipe complete in %02d:%02d                       ║\033[0m' "$(( TOTAL/60 ))" "$(( TOTAL%60 ))")
$(printf '\033[1;32m╚════════════════════════════════════════════════════════════╝\033[0m')
EOF
  # Clear local state that references the just-deleted Azure/Entra resources.
  wipe_local_state

  if [[ "$WIPE_RG_NOWAIT" != "1" ]]; then
    if az group show --name "$AZ_RG" -o none 2>/dev/null; then
      warn "Resource group '$AZ_RG' still present — deletion may still be finalizing."
    else
      ok "Resource group '$AZ_RG' is gone. Rebuild with a single command: bash build.sh  (foundation auto-created)"
    fi
  fi
  exit 0
fi

# ======================================================================================
# Optional graceful per-phase teardown (WIPE_DELETE_RG=0): keep the empty RG shell.
# ======================================================================================
# This isolated stack has no purchased PSTN number, so a full teardown (delete ACS) is the
# default. Set ACS_FORCE_DELETE=0 to preserve ACS across a wipe/rebuild.
export ACS_FORCE_DELETE="${ACS_FORCE_DELETE:-1}"
# Fast-iteration mode: KEEP_PLATFORM=1 preserves the Phase 1 platform (UAMI + Log Analytics).
export KEEP_PLATFORM="${KEEP_PLATFORM:-0}"

warn "--keep-rg was passed — per-phase teardown; the resource group $AZ_RG will be left in place."

run_wave down phase5-rag                        || warn "Wave 1 had failures (continuing)"
run_wave down phase10-vmhost phase3-data        || warn "Wave 2 had failures (continuing)"
run_wave down phase2-ai                         || warn "Wave 3 had failures (continuing)"
if [[ "$KEEP_PLATFORM" == "1" ]]; then
  warn "KEEP_PLATFORM=1 — preserving Phase 1 platform (UAMI + Log Analytics)."
else
  run_wave down phase1-platform                 || warn "Wave 4 had failures (continuing)"
  run_wave down phase0-foundation               || warn "Wave 5 had failures (continuing)"
fi
TOTAL=$(( $(date +%s) - T_START ))

cat <<EOF

$(printf '\033[1;32m╔════════════════════════════════════════════════════════════╗\033[0m')
$(printf '\033[1;32m║   Per-phase wipe complete in %02d:%02d                           ║\033[0m' "$(( TOTAL/60 ))" "$(( TOTAL%60 ))")
$(printf '\033[1;32m╚════════════════════════════════════════════════════════════╝\033[0m')

Remaining resources in RG $AZ_RG (should be empty after a full per-phase wipe):
EOF
az resource list -g "$AZ_RG" --query "[].{Name:name, Type:type}" -o table 2>/dev/null || true
echo ""
# Clear local state that references torn-down resources (keeps phase1 outputs if platform kept).
wipe_local_state
if [[ "${KEEP_PLATFORM:-0}" == "1" ]]; then
  ok "Foundation kept (RG + Log Analytics / UAMI). Re-run a demo: bash build.sh"
else
  ok "Done. Re-run the demo with a single command: bash build.sh  (foundation auto-created if missing)"
fi
