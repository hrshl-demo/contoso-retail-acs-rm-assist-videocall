#!/usr/bin/env bash
# infra/common/setup_docker_cache.sh
#
# Key Vault-free base-image caching for the CRM frontend build.
#
# The CRM frontend Dockerfile bases on docker.io/nginxinc/nginx-unprivileged, which is
# subject to Docker Hub's 100/6h anonymous-pull rate limit. To avoid that WITHOUT using
# Key Vault (a subscription policy can lock KV public access, which breaks the older
# ACR credential-set + cache-rule approach), this script simply imports the base image
# into the project ACR ONCE using inline Docker Hub credentials:
#
#     az acr import --source docker.io/... --username <u> --password <t>
#
# The frontend build (phase6) then pulls the base from ${ACR_LOGIN_SERVER}/... instead
# of Docker Hub. If credentials are absent or the import fails, the build still works —
# phase6 falls back to pulling the base image straight from Docker Hub.
#
# Reads Docker Hub credentials from $HOME/.docker_cred. Expected format:
#
#     DOCKER_USERNAME="youruser"
#     DOCKER_TOKEN="dckr_pat_..."
#
# Idempotent: if the tag is already present in ACR, the import is skipped.

set -euo pipefail

if [[ -z "${PHASE:-}" ]]; then
  export PHASE="docker-cache"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./env.sh
source "$SCRIPT_DIR/env.sh"

CRED_FILE="${DOCKER_CRED_FILE:-$HOME/.docker_cred}"
SOURCE_REPO="docker.io/nginxinc/nginx-unprivileged"
TARGET_REPO="nginxinc/nginx-unprivileged"
WARMUP_TAG="${DOCKER_CACHE_WARMUP_TAG:-1.27-alpine}"

log "Docker base-image import — checking prerequisites"

PHASE1_OUT="$SCRIPT_DIR/../phase1-platform/outputs.env"
if [[ ! -f "$PHASE1_OUT" ]]; then
  die "Phase 1 outputs missing — run phase1 before docker cache setup"
fi
# shellcheck disable=SC1090
source "$PHASE1_OUT"

if [[ ! -f "$CRED_FILE" ]]; then
  warn "No Docker Hub credentials at $CRED_FILE — skipping base-image import."
  warn "Frontend builds may hit Docker Hub anonymous-pull rate limit (100/6h)."
  warn "To enable caching, create $CRED_FILE with:"
  warn "    DOCKER_USERNAME=\"yourusername\""
  warn "    DOCKER_TOKEN=\"dckr_pat_...\""
  return 0 2>/dev/null || exit 0
fi

# shellcheck disable=SC1090
source "$CRED_FILE"

if [[ -z "${DOCKER_USERNAME:-}" || -z "${DOCKER_TOKEN:-}" ]]; then
  die "$CRED_FILE missing DOCKER_USERNAME or DOCKER_TOKEN"
fi

ok "Loaded creds for Docker Hub user: $DOCKER_USERNAME"
ensure_az_login
ensure_rg

# ────────────────────────────────────────────────────────────────
# Import the base image into ACR (authenticated — no Docker Hub anon rate limit).
# Key Vault-free: inline credentials with `az acr import`, so it works even when a
# subscription policy locks down Key Vault public access.
# ────────────────────────────────────────────────────────────────
log "Ensuring $TARGET_REPO:$WARMUP_TAG is present in ACR $NAME_ACR ..."

EXISTING_TAG="$(az acr repository show-tags --name "$NAME_ACR" --repository "$TARGET_REPO" \
  --query "[?@=='$WARMUP_TAG'] | [0]" -o tsv 2>/dev/null || echo "")"

if [[ -n "$EXISTING_TAG" ]]; then
  ok "$TARGET_REPO:$WARMUP_TAG already in ACR — skipping import"
else
  log "Importing $SOURCE_REPO:$WARMUP_TAG into $NAME_ACR (authenticated) ..."
  if az acr import \
      --name "$NAME_ACR" \
      --source "$SOURCE_REPO:$WARMUP_TAG" \
      --image "$TARGET_REPO:$WARMUP_TAG" \
      --username "$DOCKER_USERNAME" --password "$DOCKER_TOKEN" \
      --only-show-errors -o none; then
    ok "Imported $TARGET_REPO:$WARMUP_TAG into $NAME_ACR"
  else
    warn "az acr import failed (image may already exist, or Docker Hub auth issue)."
    warn "Frontend builds will fall back to pulling the base image from Docker Hub directly."
    return 0 2>/dev/null || exit 0
  fi
fi

# Final sanity: list what tags are now in the target repo
log "ACR cache state for $TARGET_REPO:"
az acr repository show-tags --name "$NAME_ACR" --repository "$TARGET_REPO" -o table 2>/dev/null \
  || warn "  (no tags cached yet)"

log "Docker base-image import complete. Frontend builds pull the nginx base from"
log "$ACR_LOGIN_SERVER/$TARGET_REPO (cached in ACR, no Docker Hub round-trip)."