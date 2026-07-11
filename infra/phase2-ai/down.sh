#!/usr/bin/env bash
# infra/phase2-ai/down.sh
#
# Phase 2 — Teardown (per-phase mode; used only when WIPE_DELETE_RG=0).
# The canonical wipe deletes the WHOLE resource group. This script is the graceful
# per-resource path that keeps the (empty) RG shell. In the self-contained build EVERY
# Phase-2 resource was created by us, so this deletes them all: chat + embedding
# deployments, the AIServices (AI Foundry) account + project, Speech, AI Search, ACS + Email.
# All are tagged project=$PROJECT_TAG_VALUE and guarded by assert_project_tag before deletion.

set -euo pipefail
PHASE="phase2"
export PHASE

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../common/env.sh
source "$SCRIPT_DIR/../common/env.sh"

log "Phase 2 — Teardown"
ensure_az_login
ensure_rg

# Opt-in safety net: never delete a deployment the operator explicitly PROTECTED via
# AOAI_CHAT_PROTECTED_DEPLOYMENTS. Empty by default in this self-contained build, so
# nothing is protected — every deployment (chat ptu AND payg, plus embedding) is torn down.
_is_protected_deployment() {
  local name="$1" prot
  for prot in ${AOAI_CHAT_PROTECTED_DEPLOYMENTS:-}; do
    [[ "$name" == "$prot" ]] && return 0
  done
  return 1
}

# Need Phase 1 outputs for UAMI principal
PHASE1_OUT="$SCRIPT_DIR/../phase1-platform/outputs.env"
if [[ -f "$PHASE1_OUT" ]]; then
  # shellcheck source=../phase1-platform/outputs.env
  source "$PHASE1_OUT"
fi

cat <<EOF

$(printf '\033[1;33m================ The following WILL be deleted ================\033[0m')
  All model deployments:        every chat + embedding deployment on the account
                                (gpt-4.1-mini ptu AND payg — deleted regardless of --type)
  AIServices (AI Foundry) acct: $NAME_AISERVICES  (+ project $NAME_FOUNDRY_PROJECT, cascades)
  Speech account:               $NAME_SPEECH
  AI Search:                    $NAME_SEARCH
  ACS:                          $NAME_ACS $([ "${ACS_FORCE_DELETE:-1}" == "1" ] && echo "(deleted)" || echo "(PRESERVED)")
  Email Comm Services:          $NAME_ACS_EMAIL  (+ AzureManagedDomain)

$(printf '\033[1;32mNothing is pre-existing or shared — this build created it all.\033[0m')
$(printf '\033[1;33m===============================================================\033[0m')

EOF
confirm "Proceed with Phase 2 teardown?"

# ---------- Model deployments — delete EVERY deployment on the account (DEPLOY_TYPE-agnostic) ----------
# CRITICAL: wipe.sh does NOT remember whether the stack was built --type=ptu or --type=payg
# (DEPLOY_TYPE defaults to 'ptu' on a bare `wipe.sh`). If we only deleted the deployment
# matching the *current* DEPLOY_TYPE, a PAYG stack wiped without --type=payg would leave
# gpt-4.1-mini-payg (and its billing) behind. So we enumerate and delete EVERY deployment on
# the account — chat (ptu/payg) and embedding — regardless of --type. This also stops billing
# fast, before the slower account delete below. Names in AOAI_CHAT_PROTECTED_DEPLOYMENTS are skipped.
log "Enumerating model deployments on $NAME_AISERVICES ..."
_DEPLOYMENTS="$(az cognitiveservices account deployment list \
  --name "$NAME_AISERVICES" -g "$AZ_RG" --query "[].name" -o tsv 2>/dev/null || true)"
if [[ -z "${_DEPLOYMENTS//[$'\t\r\n ']/}" ]]; then
  ok "No model deployments found on $NAME_AISERVICES (account absent or already emptied)."
else
  while IFS= read -r _dep; do
    [[ -n "$_dep" ]] || continue
    if _is_protected_deployment "$_dep"; then
      ok "Deployment '$_dep' is PROTECTED (AOAI_CHAT_PROTECTED_DEPLOYMENTS) — NOT deleted."
      continue
    fi
    log "Deleting deployment '$_dep' from $NAME_AISERVICES ..."
    az cognitiveservices account deployment delete \
      --name "$NAME_AISERVICES" -g "$AZ_RG" \
      --deployment-name "$_dep" -o none \
      && ok "Deleted deployment: $_dep (PTU/PAYG billing stopped)" \
      || warn "Could not delete deployment '$_dep' — it will be removed with the account below."
  done <<< "$_DEPLOYMENTS"
fi

# ---------- ACS + Email and AI Search — independent deletes (parallelizable) ----------
# HOTFIX v0.14.4 context: ACS may hold a purchased PSTN number. In THIS isolated stack
# ACS is created fresh with NO purchased number, so wipe deletes it by default
# (ACS_FORCE_DELETE=1). Set ACS_FORCE_DELETE=0 to preserve ACS + Email across a wipe.
_del_acs_email() {
  if [[ "${ACS_FORCE_DELETE:-0}" == "1" ]]; then
    warn "ACS_FORCE_DELETE=1 — deleting ACS AND releasing any purchased number."
    log "Deleting ACS: $NAME_ACS"
    local acs_id email_id
    acs_id="$(az communication list -g "$AZ_RG" --query "[?name=='${NAME_ACS}'].id | [0]" -o tsv 2>/dev/null || true)"
    if [[ -n "$acs_id" ]]; then
      assert_project_tag "$acs_id"
      az communication delete --name "$NAME_ACS" -g "$AZ_RG" --yes --only-show-errors 2>/dev/null \
        || az resource delete --ids "$acs_id" --only-show-errors
      ok "Deleted ACS"
    else
      warn "ACS not found (skipping): $NAME_ACS"
    fi
    log "Deleting Email Comm Services: $NAME_ACS_EMAIL"
    email_id="$(az resource show \
      --resource-type Microsoft.Communication/emailServices \
      --name "$NAME_ACS_EMAIL" \
      -g "$AZ_RG" \
      --query id -o tsv 2>/dev/null || true)"
    if [[ -n "$email_id" ]]; then
      assert_project_tag "$email_id"
      az resource delete --ids "$email_id" --only-show-errors && ok "Deleted Email Comm Services" || warn "Email delete failed: $NAME_ACS_EMAIL"
    else
      warn "Email Comm Services not found (skipping): $NAME_ACS_EMAIL"
    fi
  else
    ok "PRESERVING ACS ($NAME_ACS) and its phone number. Email Comm Services kept too (ACS linkedDomains dependency). Set ACS_FORCE_DELETE=1 to override."
  fi
}
_del_search() {
  log "Deleting AI Search: $NAME_SEARCH"
  local search_id
  search_id="$(az search service show -n "$NAME_SEARCH" -g "$AZ_RG" --query id -o tsv 2>/dev/null || true)"
  if [[ -n "$search_id" ]]; then
    assert_project_tag "$search_id"
    az search service delete --name "$NAME_SEARCH" -g "$AZ_RG" --yes --only-show-errors && ok "Deleted AI Search" || warn "AI Search delete failed: $NAME_SEARCH"
  else
    warn "AI Search not found (skipping): $NAME_SEARCH"
  fi
}

if [[ "${WIPE_PARALLEL_DELETES:-1}" == "1" ]]; then
  log "Deleting ACS/Email and AI Search IN PARALLEL (WIPE_PARALLEL_DELETES=1)..."
  _del_acs_email & PID_ACS=$!
  _del_search    & PID_SEARCH=$!
  wait "$PID_ACS"    || warn "ACS/Email teardown reported issues."
  wait "$PID_SEARCH" || warn "AI Search teardown reported issues."
else
  _del_acs_email
  _del_search
fi

# ---------- Speech account ----------
log "Deleting Speech account: $NAME_SPEECH"
SPEECH_ID="$(az cognitiveservices account show -n "$NAME_SPEECH" -g "$AZ_RG" --query id -o tsv 2>/dev/null || true)"
if [[ -n "$SPEECH_ID" ]]; then
  assert_project_tag "$SPEECH_ID"
  az cognitiveservices account delete -n "$NAME_SPEECH" -g "$AZ_RG" -o none \
    && ok "Deleted Speech account: $NAME_SPEECH" \
    || warn "Speech delete failed: $NAME_SPEECH"
  if [[ "${WIPE_PURGE_SOFT_DELETED:-1}" == "1" ]]; then
    az cognitiveservices account purge -n "$NAME_SPEECH" -g "$AZ_RG" -l "$AZ_REGION_SPEECH" -o none 2>/dev/null \
      && ok "Purged soft-deleted Speech account: $NAME_SPEECH" || true
  fi
else
  warn "Speech account not found (skipping): $NAME_SPEECH"
fi

# ---------- AIServices (AI Foundry) account + project (project cascades with the account) ----------
log "Deleting AIServices (AI Foundry) account: $NAME_AISERVICES  (project $NAME_FOUNDRY_PROJECT cascades)"
AISERVICES_ID="$(az cognitiveservices account show -n "$NAME_AISERVICES" -g "$AZ_RG" --query id -o tsv 2>/dev/null || true)"
if [[ -n "$AISERVICES_ID" ]]; then
  assert_project_tag "$AISERVICES_ID"
  az cognitiveservices account delete -n "$NAME_AISERVICES" -g "$AZ_RG" -o none \
    && ok "Deleted AIServices account: $NAME_AISERVICES" \
    || warn "AIServices delete failed: $NAME_AISERVICES"
  if [[ "${WIPE_PURGE_SOFT_DELETED:-1}" == "1" ]]; then
    az cognitiveservices account purge -n "$NAME_AISERVICES" -g "$AZ_RG" -l "$AZ_REGION" -o none 2>/dev/null \
      && ok "Purged soft-deleted AIServices account: $NAME_AISERVICES (name freed for redeploy)" || true
  fi
else
  warn "AIServices account not found (skipping): $NAME_AISERVICES"
fi

# ---------- Clean up outputs file ----------
rm -f "$SCRIPT_DIR/outputs.env"

ok "Phase 2 teardown complete. All Phase-2 resources (Foundry account/project/deployments included) removed."
