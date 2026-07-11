#!/usr/bin/env bash
# infra/phase9-videoassist/down.sh — delete ONLY what phase9 created (tag-guarded).
#
# Deletes the videoassist-web Container App. PRESERVES the shared platform (owned by
# other phases / deleted only by the whole-RG wipe):
#   - the ACR, UAMI, Container Apps env (owned by phase1)
#   - the AI Foundry account (created by phase2; removed by the whole-RG wipe)
#   - the Video Assist ACS resource (kept by default; remove only with ACS_FORCE_DELETE=1,
#     mirroring the project-wide ACS preservation rule).
set -uo pipefail
PHASE="phase9"; export PHASE
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=../common/env.sh
source "$SCRIPT_DIR/../common/env.sh"
VA_APP="$NAME_CA_VIDEOASSIST"
ACS_NAME="$NAME_ACS_VIDEO"
ACS_FORCE_DELETE="${ACS_FORCE_DELETE:-1}"

log "Phase 9 — Teardown (Video Assist)"
ensure_az_login; ensure_rg

APP_ID="$(az containerapp show -g "$AZ_RG" -n "$VA_APP" --query id -o tsv 2>/dev/null || true)"
if [[ -n "$APP_ID" ]]; then
  # Safety: prefer to confirm our project tag, but DO NOT abort the wipe if the tag is
  # missing — videoassist-web is a fixed, project-owned name we always create here, and
  # leaving it behind would block phase1 from deleting the shared cae-msme environment.
  TAG="$(az resource show --ids "$APP_ID" --query "tags.${PROJECT_TAG_KEY}" -o tsv 2>/dev/null || true)"
  [[ "$TAG" == "$PROJECT_TAG_VALUE" ]] || warn "$VA_APP missing tag ${PROJECT_TAG_KEY}=${PROJECT_TAG_VALUE} (got '${TAG:-none}') — deleting anyway (project-owned name)."
  az containerapp delete -g "$AZ_RG" -n "$VA_APP" --yes --only-show-errors && ok "Deleted: $VA_APP" || warn "Delete reported an error for $VA_APP"
else
  warn "Not found (skipping): $VA_APP"
fi

# A stray standalone env (videoassist-env) is NOT used by the merged deploy (we reuse
# cae-msme), but remove it if a previous standalone ./deploy.sh left one empty behind.
if az containerapp env show -n "videoassist-env" -g "$AZ_RG" -o none 2>/dev/null; then
  az containerapp env delete -n "videoassist-env" -g "$AZ_RG" --yes --only-show-errors 2>/dev/null \
    && ok "Deleted stray env videoassist-env" || warn "Could not delete videoassist-env (may be non-empty)"
fi

if [[ "$ACS_FORCE_DELETE" == "1" ]]; then
  if az communication show -n "$ACS_NAME" -g "$AZ_RG" -o none 2>/dev/null; then
    az communication delete -n "$ACS_NAME" -g "$AZ_RG" --yes --only-show-errors 2>/dev/null \
      && ok "Deleted ACS $ACS_NAME (ACS_FORCE_DELETE=1)" || warn "ACS delete skipped/failed"
  fi
else
  log "ACS '$ACS_NAME' preserved (set ACS_FORCE_DELETE=1 to remove)."
fi

rm -f "$SCRIPT_DIR/outputs.env"
ok "Phase 9 teardown complete."
