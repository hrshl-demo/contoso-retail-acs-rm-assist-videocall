#!/usr/bin/env bash
# infra/common/env.sh
#
# SINGLE SOURCE OF TRUTH for the Contoso Retail "RM Assist — Rakesh Sharma" demo.
# EVERY configurable value the deploy/wipe scripts need lives in this one file, and
# is PRE-FILLED with the demo values below. No other script hardcodes configuration —
# they all `source` this file. To retarget the demo, edit ONLY this file.
#
# Source it (do not execute) from any phase script:
#   source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../common/env.sh"
#
# What this demo builds (lean, single-journey):
#   The "Everyday RM Assist" journey for retail customer Rakesh Sharma (CTB-RTL-002),
#   including the live, AI-coached VIDEO CALL (Step 7). This is a FULLY SELF-CONTAINED,
#   ALL-OR-NOTHING build: it CREATES its own resource group and EVERYTHING inside it —
#   including the AI Foundry (AIServices) account + project, the gpt-4.1-mini chat
#   deployment (PAYG/GlobalStandard by default, or PTU with `--type=ptu`), and the
#   text-embedding-3-small deployment — and DELETES all of it (the whole resource group)
#   on wipe. NOTHING is pre-provisioned or reused; there are no pre-coded resource IDs
#   to depend on.

# Strict mode for the phase SCRIPTS that source this file (they run non-interactively),
# but NOT for an interactive shell (so a stray unset var can't kill your session).
case $- in
  *i*) : ;;                  # interactive — leave the user's shell options alone
  *)   set -euo pipefail ;;  # script context — keep strict mode
esac

# Auto-generated secrets (written by ./setup-graph.sh) are sourced FIRST so their exported
# values win over the ${VAR:-} defaults further down. This file is git-ignored and is how
# the fully-automated Microsoft Graph (RM calendar) credentials reach the deploy — never
# commit real secrets.
__ENV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$__ENV_DIR/secrets.env" ]]; then
  # shellcheck disable=SC1091
  source "$__ENV_DIR/secrets.env"
fi

# =====================================================================================
# 1) CORE AZURE CONTEXT
# =====================================================================================
export AZ_SUBSCRIPTION_ID="${AZ_SUBSCRIPTION_ID:-ce9b822d-f1a4-45f4-ac2c-f2255ba5dbd8}"
export AZURE_SUBSCRIPTION_ID="$AZ_SUBSCRIPTION_ID"
export AZ_TENANT_ID="${AZ_TENANT_ID:-5cc1cdba-5904-4909-bf6a-2289c50333fb}"

# Resource group + region are CREATED and DELETED by this stack (all-or-nothing).
# deploy.sh creates $AZ_RG in $AZ_REGION; wipe.sh deletes the ENTIRE resource group.
# There is no pre-existing RG assumption — override AZ_RG/AZ_REGION here to retarget.
export AZ_REGION="${AZ_REGION:-southindia}"
export AZURE_LOCATION="$AZ_REGION"
export AZ_RG="${AZ_RG:-rg-contoso-rmx-rakesh}"

# Azure AI Search is region-bound and not offered in every region; South India in
# particular may lack Search capacity, so it defaults to a nearby region. Override freely.
export AZ_REGION_SEARCH="${AZ_REGION_SEARCH:-centralindia}"

# The standalone Speech (SpeechServices kind) account is NOT offered in South India
# (ARM rejects it with InvalidApiSetId). It therefore has its own region, defaulting to a
# nearby Speech-capable region. The in-call STT token path (Video Assist) targets this
# account + region, so keep them consistent. Override freely.
export AZ_REGION_SPEECH="${AZ_REGION_SPEECH:-centralindia}"

# =====================================================================================
# 2) AI FOUNDRY  (CREATED + DELETED by this stack — see section 5 for the actual names)
# =====================================================================================
# The AI Foundry (AIServices, kind=AIServices) account and its project are provisioned
# fresh inside $AZ_RG by phase2-ai (Bicep), and removed with the resource group on wipe.
# Their names + derived resource IDs/endpoints are defined in SECTION 5, because they
# incorporate the globally-unique $SUFFIX which is computed in section 3 below.

# =====================================================================================
# 3) PROJECT IDENTITY  (NEW — isolates this stack so wipe only ever touches OUR resources)
# =====================================================================================
# The teardown safety guard (assert_project_tag) keys off PROJECT_TAG_VALUE, so this
# MUST be distinct from any other project sharing the resource group.
export PROJECT_TAG_KEY="project"
export PROJECT_TAG_VALUE="contoso-retail-rm-assist-rakesh"
export PROJECT_TAG="${PROJECT_TAG_KEY}=${PROJECT_TAG_VALUE}"

# Deterministic 5-char suffix (stable across re-runs on this host, unique per-project).
# Used in globally-unique names (ACR, AI Search, ACS).
if [[ -z "${SUFFIX:-}" ]]; then
  SUFFIX="$(printf '%s|%s|%s' "$AZ_SUBSCRIPTION_ID" "$AZ_RG" "$PROJECT_TAG_VALUE" \
    | sha256sum | cut -c1-5)"
fi
export SUFFIX

# =====================================================================================
# 4) CHAT MODEL — selected by DEPLOY_TYPE  (default 'payg'; 'ptu' via `build.sh --type=ptu`)
# =====================================================================================
# Two mutually-exclusive profiles, chosen by DEPLOY_TYPE. In THIS self-contained build the
# chat deployment is ALWAYS created on the freshly-provisioned Foundry account and ALWAYS
# deleted with the resource group on wipe — the only difference is the SKU that gets created:
#
#   payg (DEFAULT) — CREATE a pay-as-you-go gpt-4.1-mini deployment (GlobalStandard).
#                    Bills per token used, needs only GlobalStandard quota, and is what a
#                    bare `bash build.sh` produces.
#   ptu            — CREATE a 15-PTU gpt-4.1-mini deployment (GlobalProvisionedManaged).
#                    Bills per hour while it exists and needs provisioned-throughput quota.
#                    Selected with:  bash build.sh --type=ptu
#
# DEPLOY_TYPE is exported by build.sh / deploy.sh / wipe.sh from the --type flag; default is 'payg'.
export DEPLOY_TYPE="${DEPLOY_TYPE:-payg}"
if [[ "$DEPLOY_TYPE" != "ptu" && "$DEPLOY_TYPE" != "payg" ]]; then
  printf '\033[1;33m[!]\033[0m Unknown DEPLOY_TYPE="%s" — falling back to "payg".\n' "$DEPLOY_TYPE" >&2
  DEPLOY_TYPE="payg"; export DEPLOY_TYPE
fi

# Common model coordinates (identical for both profiles).
export AOAI_CHAT_MODEL_NAME="${AOAI_CHAT_MODEL_NAME:-gpt-4.1-mini}"
export AOAI_CHAT_MODEL_FORMAT="${AOAI_CHAT_MODEL_FORMAT:-OpenAI}"
export AOAI_CHAT_MODEL_VERSION="${AOAI_CHAT_MODEL_VERSION:-}"   # empty => auto-discover latest available version

# --- PAYG profile knobs (used only when DEPLOY_TYPE=payg) ---------------------------
# CREATED here + DELETED on wipe (a GlobalStandard, pay-per-token deployment).
export AOAI_CHAT_PAYG_DEPLOYMENT_NAME="${AOAI_CHAT_PAYG_DEPLOYMENT_NAME:-gpt-4.1-mini-payg}"
export AOAI_CHAT_PAYG_SKU_NAME="${AOAI_CHAT_PAYG_SKU_NAME:-GlobalStandard}"
export AOAI_CHAT_PAYG_SKU_CAPACITY="${AOAI_CHAT_PAYG_SKU_CAPACITY:-50}"   # ×1K tokens/min quota units

# --- PTU profile knobs (used only when DEPLOY_TYPE=ptu) -----------------------------
export AOAI_CHAT_PTU_DEPLOYMENT_NAME="${AOAI_CHAT_PTU_DEPLOYMENT_NAME:-gpt-4.1-mini-ptu}"
export AOAI_CHAT_PTU_SKU_NAME="${AOAI_CHAT_PTU_SKU_NAME:-GlobalProvisionedManaged}"        # PTU (Provisioned Throughput)
export AOAI_CHAT_PTU_SKU_CAPACITY="${AOAI_CHAT_PTU_SKU_CAPACITY:-15}"                      # 15 PTU

# Resolve the ACTIVE chat deployment (name + SKU + capacity) from the selected profile.
# Both profiles are lifecycle-managed (created by phase2, deleted with the RG on wipe).
if [[ "$DEPLOY_TYPE" == "payg" ]]; then
  export AOAI_CHAT_DEPLOYMENT_NAME="$AOAI_CHAT_PAYG_DEPLOYMENT_NAME"
  export AOAI_CHAT_SKU_NAME="$AOAI_CHAT_PAYG_SKU_NAME"
  export AOAI_CHAT_SKU_CAPACITY="$AOAI_CHAT_PAYG_SKU_CAPACITY"
else
  export AOAI_CHAT_DEPLOYMENT_NAME="$AOAI_CHAT_PTU_DEPLOYMENT_NAME"
  export AOAI_CHAT_SKU_NAME="$AOAI_CHAT_PTU_SKU_NAME"
  export AOAI_CHAT_SKU_CAPACITY="$AOAI_CHAT_PTU_SKU_CAPACITY"
fi
export AOAI_CHAT_MANAGE_LIFECYCLE="1"                 # ALWAYS create (phase2) + delete (wipe)

# Optional safety net (space-separated deployment names never deleted by phase2 down.sh).
# In this self-contained build the whole RG is deleted on wipe, so nothing is protected
# by default. Populate it only if you point this stack at a shared account you don't own.
export AOAI_CHAT_PROTECTED_DEPLOYMENTS="${AOAI_CHAT_PROTECTED_DEPLOYMENTS:-}"

# The rest of the stack references the chat deployment by this name.
export EXISTING_AOAI_CHAT_DEPLOYMENT="$AOAI_CHAT_DEPLOYMENT_NAME"

# --- VOICE model — a THIRD, dedicated deployment for the live-call intelligence -------
# The in-call path (fast nudge classifier, answer/tool planner, synopsis, case logging)
# runs on a REASONING model. The CRM / backend narration path is untouched and stays on
# $AOAI_CHAT_MODEL_NAME. DEPLOY_TYPE (ptu|payg) continues to govern ONLY the chat
# deployment above — it does not apply here.
#
# IDEMPOTENCY WARNING: phase2-ai/up.sh treats a deployment as "already there" by
# DEPLOYMENT NAME, not by model. If you change AOAI_VOICE_MODEL_NAME you MUST also
# change AOAI_VOICE_DEPLOYMENT_NAME, otherwise the old model is silently reused.
# Azure deployment names may not contain '.', hence "gpt-54-mini-voice".
export VOICE_MODEL_ENABLED="${VOICE_MODEL_ENABLED:-1}"   # EDIT HERE: set to 0 to send the live-call path back to the chat deployment
export AOAI_VOICE_MODEL_NAME="${AOAI_VOICE_MODEL_NAME:-gpt-5.4-mini}"
export AOAI_VOICE_MODEL_FORMAT="${AOAI_VOICE_MODEL_FORMAT:-OpenAI}"
export AOAI_VOICE_MODEL_VERSION="${AOAI_VOICE_MODEL_VERSION:-}"   # empty => auto-discover latest available version
export AOAI_VOICE_DEPLOYMENT_NAME="${AOAI_VOICE_DEPLOYMENT_NAME:-gpt-54-mini-voice}"
export AOAI_VOICE_SKU_NAME="${AOAI_VOICE_SKU_NAME:-GlobalStandard}"
export AOAI_VOICE_SKU_CAPACITY="${AOAI_VOICE_SKU_CAPACITY:-50}"   # ×1K tokens/min quota units
# Reasoning models bill their hidden reasoning tokens out of max_completion_tokens, so a
# low effort keeps in-call latency inside the nudge freshness window.
export VOICE_AI_REASONING_EFFORT="${VOICE_AI_REASONING_EFFORT:-low}"
export EXISTING_AOAI_VOICE_DEPLOYMENT="$AOAI_VOICE_DEPLOYMENT_NAME"

# --- Embedding model — CREATED + DELETED (fresh, powers RAG/SOP search in phase5) -----
export AOAI_EMBED_MODEL_NAME="${AOAI_EMBED_MODEL_NAME:-text-embedding-3-small}"
export AOAI_EMBED_MODEL_FORMAT="${AOAI_EMBED_MODEL_FORMAT:-OpenAI}"
export AOAI_EMBED_MODEL_VERSION="${AOAI_EMBED_MODEL_VERSION:-}"   # empty => auto-discover latest
export AOAI_EMBED_DEPLOYMENT_NAME="${AOAI_EMBED_DEPLOYMENT_NAME:-text-embedding-3-small}"
# SKU note: text-embedding-3-small is offered as GlobalStandard in southindia (the plain
# 'Standard' SKU is NOT available there — ARM returns InvalidResourceProperties). GlobalStandard
# is also valid in the usual PTU regions, so it is the portable default. Override per region if needed.
export AOAI_EMBED_SKU_NAME="${AOAI_EMBED_SKU_NAME:-GlobalStandard}"
export AOAI_EMBED_SKU_CAPACITY="${AOAI_EMBED_SKU_CAPACITY:-50}"   # ×1K tokens/min quota units
# RAG degrades gracefully if this deployment is somehow absent.
export EXISTING_AOAI_EMBED_DEPLOYMENT="$AOAI_EMBED_DEPLOYMENT_NAME"

# =====================================================================================
# 5) NET-NEW RESOURCE NAMES  (CREATED + DELETED by this stack)
# =====================================================================================
# --- AI Foundry (AIServices) account + project — CREATED + DELETED by this stack ---
# The account name is ALSO its custom subdomain, so it must be globally unique (hence the
# suffix). Downstream phases reference these via the EXISTING_* aliases below, which now
# point at the freshly-created resources.
export NAME_AISERVICES="${NAME_AISERVICES:-aifndry-rmx-${SUFFIX}}"     # AIServices account (2-64, custom subdomain)
export NAME_FOUNDRY_PROJECT="${NAME_FOUNDRY_PROJECT:-proj-rmx-${SUFFIX}}"  # Foundry project (child of the account)

# Back-compat aliases consumed across the stack — now resolve to the CREATED account/project.
export EXISTING_AISERVICES_NAME="$NAME_AISERVICES"
export EXISTING_FOUNDRY_PROJECT_NAME="$NAME_FOUNDRY_PROJECT"
export AZURE_EXISTING_RESOURCE_ID="/subscriptions/${AZ_SUBSCRIPTION_ID}/resourceGroups/${AZ_RG}/providers/Microsoft.CognitiveServices/accounts/${NAME_AISERVICES}"
export AZURE_EXISTING_AIPROJECT_RESOURCE_ID="${AZURE_EXISTING_RESOURCE_ID}/projects/${NAME_FOUNDRY_PROJECT}"
export AZURE_EXISTING_AIPROJECT_ENDPOINT="https://${NAME_AISERVICES}.openai.azure.com/openai/v1/"

# Convention: <abbrev>-rmx-<purpose>[-suffix]. Globally-unique names carry the suffix.
export NAME_LAW="log-rmx"                        # Log Analytics workspace
export NAME_ACR="acrrmx${SUFFIX}"                # ACR: 5-50 chars, alphanumeric only
export NAME_UAMI="id-rmx-app"                    # user-assigned managed identity
export NAME_ACA_ENV="cae-rmx"                    # Container Apps environment

export NAME_SEARCH="srch-rmx-${SUFFIX}"          # AI Search: 2-60, lowercase+digits+hyphens
export NAME_ACS="acs-rmx-${SUFFIX}"              # ACS (video/voice tokens + telemetry); no purchased PSTN number
export NAME_SPEECH="spch-rmx-${SUFFIX}"          # Speech (ACS media-streaming fallback)

export NAME_CA_TOOLAPI="ca-rmx-toolapi"          # FastAPI Tool API (phase4)
export NAME_CA_CRM="ca-rmx-dashboard"            # CRM cockpit dashboard (phase6)

# Video Assist (Step 7 live video call).
export NAME_CA_VIDEOASSIST="videoassist-web"     # Container App name (phase9)
export NAME_ACS_VIDEO="acs-rmx-video-${SUFFIX}"  # video tokens only (no purchased PSTN number)

# AI Search index name for the SOP knowledge base (phase5 RAG).
export SEARCH_INDEX_NAME="contoso-retail-policy-index"

# =====================================================================================
# 6) VIDEO ASSIST — AI + Speech + ACS  (derived from the created Foundry account)
# =====================================================================================
# These are injected as container env vars by phase9-videoassist/up.sh. Entra auth only,
# no keys. All derived from the context above so there is a single place to change them.
export AZURE_AI_ENDPOINT="https://${EXISTING_AISERVICES_NAME}.services.ai.azure.com/openai/v1"
export AZURE_AI_CHAT_DEPLOYMENT="$EXISTING_AOAI_CHAT_DEPLOYMENT"
export AZURE_AI_EMBED_DEPLOYMENT="$EXISTING_AOAI_EMBED_DEPLOYMENT"
export AZURE_AI_SCOPE="https://ai.azure.com/.default"
# Speech (in-call STT) is served by the dedicated SpeechServices account created in
# section 5 ($NAME_SPEECH), which lives in $AZ_REGION_SPEECH (South India cannot host it).
# The Video Assist UAMI is granted "Cognitive Services User" on this account by phase2 Bicep.
export AZURE_SPEECH_REGION="${AZURE_SPEECH_REGION:-$AZ_REGION_SPEECH}"
export AZURE_SPEECH_RESOURCE_ID="${AZURE_SPEECH_RESOURCE_ID:-/subscriptions/${AZ_SUBSCRIPTION_ID}/resourceGroups/${AZ_RG}/providers/Microsoft.CognitiveServices/accounts/${NAME_SPEECH}}"
export ACS_DATA_LOCATION="${ACS_DATA_LOCATION:-India}"
# In-call synopsis/nudge model. When VOICE_MODEL_ENABLED=1 this points at the dedicated
# reasoning voice deployment created by phase2; otherwise it falls back to the
# DEPLOY_TYPE-selected chat deployment (the original behaviour).
# ROLLBACK LEVER (edit this file — no command-line flag or inline env var needed):
#   * to move the whole live-call path back to the chat model, set
#     VOICE_MODEL_ENABLED=0 on its line above;
#   * to move ONLY the latency-critical fast classifier back, replace the default on the
#     VOICE_AI_FAST_DEPLOYMENT line below with "$AOAI_CHAT_DEPLOYMENT_NAME".
# Then simply re-run `bash build.sh`.
if [[ "${VOICE_MODEL_ENABLED:-1}" == "1" ]]; then
  export VOICE_AI_CHAT_DEPLOYMENT="${VOICE_AI_CHAT_DEPLOYMENT:-$AOAI_VOICE_DEPLOYMENT_NAME}"
else
  export VOICE_AI_CHAT_DEPLOYMENT="${VOICE_AI_CHAT_DEPLOYMENT:-$AOAI_CHAT_DEPLOYMENT_NAME}"
fi
export VOICE_AI_FAST_DEPLOYMENT="${VOICE_AI_FAST_DEPLOYMENT:-$VOICE_AI_CHAT_DEPLOYMENT}"
# Deployment names (comma-separated) that must use the reasoning-model request shape
# (reasoning_effort + max_completion_tokens, no temperature). Empty when the voice model
# is disabled, which restores the original request shape exactly.
if [[ "${VOICE_MODEL_ENABLED:-1}" == "1" ]]; then
  export AI_REASONING_DEPLOYMENTS="${AI_REASONING_DEPLOYMENTS:-$AOAI_VOICE_DEPLOYMENT_NAME}"
else
  export AI_REASONING_DEPLOYMENTS="${AI_REASONING_DEPLOYMENTS:-}"
fi
export VOICE_AI_WARMUP="${VOICE_AI_WARMUP:-1}"
# Voice Live is a MANAGED MODEL IDENTIFIER (not a deployment / not a resource, no extra cost).
# Referenced by phase2 (emits it to outputs.env) and phase4 (Tool API env). The lean
# Rakesh flow does not exercise the realtime voice-live path, but the value must be defined.
export VOICELIVE_MODEL="${VOICELIVE_MODEL:-gpt-4.1}"
# Live-nudge tuning (safe defaults; override only if the demo needs different timings).
# A reasoning model spends hidden thinking time before its first token, so the
# latency-critical classifier gets a wider abort window and a wider freshness window
# when VOICE_MODEL_ENABLED=1. With VOICE_MODEL_ENABLED=0 the original timings apply.
if [[ "${VOICE_MODEL_ENABLED:-1}" == "1" ]]; then
  export FAST_NUDGE_TIMEOUT_MS="${FAST_NUDGE_TIMEOUT_MS:-7000}"
  export NUDGE_FRESHNESS_MS="${NUDGE_FRESHNESS_MS:-9000}"
else
  export FAST_NUDGE_TIMEOUT_MS="${FAST_NUDGE_TIMEOUT_MS:-3400}"
  export NUDGE_FRESHNESS_MS="${NUDGE_FRESHNESS_MS:-5500}"
fi
export FAST_PATH_HEADSTART_MS="${FAST_PATH_HEADSTART_MS:-300}"
export NUDGE_TEAMS_TIMEOUT_MS="${NUDGE_TEAMS_TIMEOUT_MS:-5000}"
export NUDGE_MIN_CONFIDENCE="${NUDGE_MIN_CONFIDENCE:-0.68}"

# =====================================================================================
# 7) DEMO RUNTIME SETTINGS
# =====================================================================================
# The retail customer Step 7 binds to when the URL omits ?customer_id.
export DEFAULT_CUSTOMER_ID="${DEFAULT_CUSTOMER_ID:-CTB-RTL-002}"   # CTB-RTL-002 = Rakesh Sharma
# RM display name shown to customers on the Step 7 self-service scheduling page.
export RM_DISPLAY_NAME="${RM_DISPLAY_NAME:-Priya Nair (Branch RM, RM-2207)}"

# ---- ACS teardown policy --------------------------------------------------------------
# This isolated stack creates FRESH ACS resources with NO purchased PSTN number, so a
# full, clean teardown is the default: wipe deletes the ACS + Email + video ACS it made.
# Set ACS_FORCE_DELETE=0 to preserve them across a wipe/rebuild cycle instead.
export ACS_FORCE_DELETE="${ACS_FORCE_DELETE:-1}"

# =====================================================================================
# 8) EXTERNAL INTEGRATION — Teams / Power Automate nudge webhook  (NOT Azure-IaC'able)
# =====================================================================================
# The only dependency this stack cannot create/destroy in Azure: a Power Automate
# "When a Teams webhook request is received" trigger URL used to post the in-call
# synopsis + live nudges into Teams. It is a SIGNED, STABLE URL (the sig= is its own
# credential; it does not rotate). Provide it WITHOUT committing it: put it in
# infra/common/secrets.env (git-ignored; copy secrets.env.example) or as the GitHub
# Actions secret TEAMS_WEBHOOK_URL. Leave empty to run the demo without Teams posting —
# the video call still works; nudges just won't appear in Teams. See docs/POWER_AUTOMATE.md.
#   Stored server-side by phase9 as the Container App secret `teams-webhook`.
export TEAMS_WEBHOOK_URL="${TEAMS_WEBHOOK_URL:-}"
# Optional dedicated minimal workflow for live nudges (falls back to TEAMS_WEBHOOK_URL).
export TEAMS_NUDGE_WEBHOOK_URL="${TEAMS_NUDGE_WEBHOOK_URL:-}"
# Optional Step 7 scheduling: RM-owned Power Automate flows for real availability +
# auto-created Teams meetings. Leave empty to serve synthetic working-hours availability
# and record bookings only.
export SCHEDULE_WEBHOOK_URL="${SCHEDULE_WEBHOOK_URL:-}"
export SCHEDULE_AVAILABILITY_WEBHOOK_URL="${SCHEDULE_AVAILABILITY_WEBHOOK_URL:-}"
# Instant "Call your RM" (customer mobile banking portal /bank). RM_MEETING_URL is the
# RM's standing Teams meeting join link — used to auto-provision the call when no
# SCHEDULE_WEBHOOK_URL flow is configured, so the customer never sees a link. Leave
# empty to fall back to a synthetic demo link. CALL_LEAD_SECONDS is the demo delay
# between the tap and the call going live (default 60).
export RM_MEETING_URL="${RM_MEETING_URL:-}"
export CALL_LEAD_SECONDS="${CALL_LEAD_SECONDS:-60}"
# Production path (optional): create a REAL Teams meeting on the RM's calendar via
# Microsoft Graph (app-only). Needs an app registration with the Calendars.ReadWrite
# APPLICATION permission (admin-consented) and RM_USER_ID = the RM's mailbox (UPN or
# objectId). When set, each customer tap creates a fresh calendar event + Teams meeting;
# both the RM (from Teams/Outlook) and the customer (via ACS interop) join the SAME link.
export GRAPH_TENANT_ID="${GRAPH_TENANT_ID:-}"
export GRAPH_CLIENT_ID="${GRAPH_CLIENT_ID:-}"
export GRAPH_CLIENT_SECRET="${GRAPH_CLIENT_SECRET:-}"
export RM_USER_ID="${RM_USER_ID:-}"
export MEETING_TIMEZONE="${MEETING_TIMEZONE:-India Standard Time}"
export MEETING_DURATION_MINUTES="${MEETING_DURATION_MINUTES:-30}"

# =====================================================================================
# 8b) PERFORMANCE / SPEED TUNABLES  (make deploy + wipe faster; all overridable)
# =====================================================================================
# These only affect WALL-CLOCK time, never what is created. Every one has a safe default
# and can be overridden from the environment (e.g. `PREBUILD_IMAGES=0 bash deploy.sh`).
#
# PREBUILD_IMAGES=1 builds all three container images (Tool API, Video Assist, CRM
#   dashboard) CONCURRENTLY right after phase1 — overlapped with phase2's slow AI Search
#   provisioning — so the later phases just deploy a ready image instead of building in
#   series. Set 0 to have each phase build its own image inline (the original behavior).
export PREBUILD_IMAGES="${PREBUILD_IMAGES:-1}"
#
# PHASE5_REBUILD_TOOLAPI=0 SKIPS phase5's Tool API image rebuild + redeploy. The image
#   phase4 already built from backend/ contains the RAG code (rag.py, search.py), so the
#   rebuild is redundant; phase5 still creates the index and uploads the SOP embeddings.
#   Set 1 to force a rebuild+redeploy in phase5 (e.g. if you changed backend code between
#   phase4 and phase5).
export PHASE5_REBUILD_TOOLAPI="${PHASE5_REBUILD_TOOLAPI:-0}"
#
# WIPE_PARALLEL_DELETES=1 deletes INDEPENDENT resources CONCURRENTLY during teardown
#   (phase1: Container Apps env / ACR / Log Analytics / UAMI; phase2: AI Search
#   / ACS / Email). The slow Container Apps environment delete then overlaps the quick ones.
#   Set 0 for the original one-at-a-time teardown (easier to read logs when debugging).
export WIPE_PARALLEL_DELETES="${WIPE_PARALLEL_DELETES:-1}"

# =====================================================================================
# 8c) BUILD RELIABILITY — split build (build_rg.sh + build.sh)
# =====================================================================================
# The build is optionally split into two runs so RG-level setup happens ONCE:
#   BUILD_STAGE=foundation  -> phase0 + phase1 only  (run by build_rg.sh; non-billable)
#   BUILD_STAGE=apps        -> phase2..phase9        (run by build.sh; billable, per demo)
#   BUILD_STAGE=all         -> everything            (run by deploy.sh; one-shot, default)
export BUILD_STAGE="${BUILD_STAGE:-all}"

# =====================================================================================
# 9) HELPERS
# =====================================================================================
log()    { printf '\033[1;34m[+]\033[0m %s\n' "$*"; }
warn()   { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()    { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }
ok()     { printf '\033[1;32m[\xe2\x9c\x93]\033[0m %s\n' "$*"; }

confirm() {
  local prompt="${1:-Proceed?}"
  read -r -p "$(printf '\033[1;33m[?]\033[0m %s [y/N]: ' "$prompt")" reply
  case "$reply" in
    y|Y|yes|YES) return 0 ;;
    *) die "Aborted by user." ;;
  esac
}

ensure_az_login() {
  local current_sub
  current_sub="$(az account show --query id -o tsv 2>/dev/null || true)"
  if [[ -z "$current_sub" ]]; then
    die "az not logged in. Run 'az login' first."
  fi
  if [[ "$current_sub" != "$AZ_SUBSCRIPTION_ID" ]]; then
    warn "Current subscription ($current_sub) != expected ($AZ_SUBSCRIPTION_ID). Switching."
    az account set --subscription "$AZ_SUBSCRIPTION_ID"
  fi
  ok "Subscription: $AZ_SUBSCRIPTION_ID"
}

ensure_rg() {
  if az group show --name "$AZ_RG" --subscription "$AZ_SUBSCRIPTION_ID" >/dev/null 2>&1; then
    local actual_region
    actual_region="$(az group show --name "$AZ_RG" --query location -o tsv)"
    if [[ "$actual_region" != "$AZ_REGION" ]]; then
      warn "RG region ($actual_region) != expected ($AZ_REGION). Continuing but new resources deploy to $AZ_REGION."
    fi
    ok "Resource group: $AZ_RG ($actual_region)"
  else
    log "Creating resource group $AZ_RG in $AZ_REGION ..."
    az group create --name "$AZ_RG" --location "$AZ_REGION" \
      --tags "${PROJECT_TAG_KEY}=${PROJECT_TAG_VALUE}" \
      --subscription "$AZ_SUBSCRIPTION_ID" --only-show-errors -o none \
      || die "Failed to create resource group $AZ_RG in $AZ_REGION."
    ok "Created resource group: $AZ_RG ($AZ_REGION)"
  fi
}

# Standard tag args for any `az ... create` or `az resource tag` call.
tag_args() {
  printf -- '--tags %s=%s phase=%s' "$PROJECT_TAG_KEY" "$PROJECT_TAG_VALUE" "${PHASE:-unknown}"
}

# Safety guard: refuse to delete anything not tagged with our project.
assert_project_tag() {
  local rid="$1"
  local tag
  tag="$(az resource show --ids "$rid" --query "tags.${PROJECT_TAG_KEY}" -o tsv 2>/dev/null || true)"
  if [[ "$tag" != "$PROJECT_TAG_VALUE" ]]; then
    die "Refusing to delete $rid — missing tag ${PROJECT_TAG_KEY}=${PROJECT_TAG_VALUE} (got '$tag')."
  fi
}

# -------------------------------------------------------------------------------------
# regen_phase1_outputs — rebuild infra/phase1-platform/outputs.env FROM AZURE if missing.
# phase2/4/5/6/9 up.sh source that file for ACR_LOGIN_SERVER / UAMI_*. When the
# billable build (build.sh, BUILD_STAGE=apps) runs in a shell/checkout where phase1 up.sh
# did not run (foundation was created earlier by build_rg.sh, or the tarball was
# re-extracted), the file may be absent — reconstruct it so the app phases don't die.
regen_phase1_outputs() {
  local root outfile
  root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  outfile="$root/phase1-platform/outputs.env"
  if [[ -s "$outfile" ]] && grep -q 'UAMI_ID=' "$outfile" 2>/dev/null; then
    return 0
  fi
  log "Reconstructing Phase 1 outputs from Azure (outputs.env missing)..."
  local acr_login uami_json uami_pid="" uami_cid="" uami_id="" cae_domain
  acr_login="$(az acr show -n "$NAME_ACR" -g "$AZ_RG" --query loginServer -o tsv 2>/dev/null || true)"
  uami_json="$(az identity show -n "$NAME_UAMI" -g "$AZ_RG" -o json 2>/dev/null || true)"
  if [[ -n "$uami_json" ]]; then
    uami_pid="$(echo "$uami_json" | jq -r '.principalId // empty')"
    uami_cid="$(echo "$uami_json" | jq -r '.clientId // empty')"
    uami_id="$(echo "$uami_json" | jq -r '.id // empty')"
  fi
  cae_domain="$(az containerapp env show -n "$NAME_ACA_ENV" -g "$AZ_RG" --query properties.defaultDomain -o tsv 2>/dev/null || true)"
  if [[ -z "$acr_login" || -z "$uami_id" ]]; then
    die "Could not reconstruct Phase 1 outputs from Azure (ACR/UAMI missing). Run 'bash build_rg.sh' first."
  fi
  cat > "$outfile" <<EOF
# Regenerated from Azure by env.sh:regen_phase1_outputs on $(date -u --iso-8601=seconds)
export ACR_LOGIN_SERVER="$acr_login"
export UAMI_PRINCIPAL_ID="$uami_pid"
export UAMI_CLIENT_ID="$uami_cid"
export UAMI_ID="$uami_id"
export CAE_DEFAULT_DOMAIN="$cae_domain"
EOF
  ok "Reconstructed Phase 1 outputs -> $outfile"
}

# -------------------------------------------------------------------------------------
# foundation_present — NON-FATAL predicate. Returns 0 if the non-billable foundation
# (RG + ACR + UAMI + Container Apps env) is fully present, 1 if anything is missing.
# Sets the global array FOUNDATION_MISSING to the human-readable names that are absent,
# so callers can decide whether to auto-build it or fail. Prints nothing.
foundation_present() {
  FOUNDATION_MISSING=()
  az group show -n "$AZ_RG" -o none 2>/dev/null                              || FOUNDATION_MISSING+=("resource group $AZ_RG")
  az acr show -n "$NAME_ACR" -g "$AZ_RG" -o none 2>/dev/null                  || FOUNDATION_MISSING+=("ACR $NAME_ACR")
  az identity show -n "$NAME_UAMI" -g "$AZ_RG" -o none 2>/dev/null            || FOUNDATION_MISSING+=("UAMI $NAME_UAMI")
  az containerapp env show -n "$NAME_ACA_ENV" -g "$AZ_RG" -o none 2>/dev/null || FOUNDATION_MISSING+=("Container Apps env $NAME_ACA_ENV")
  (( ${#FOUNDATION_MISSING[@]} == 0 ))
}

# assert_foundation_present — verify the non-billable foundation exists before the billable
# app build (build.sh). Dies with clear guidance if anything is missing, then regenerates
# phase1 outputs.env if needed. Callers that prefer to self-heal should test
# foundation_present first and build phase0+phase1 themselves (see rebuild-parallel.sh).
assert_foundation_present() {
  if ! foundation_present; then
    warn "Foundation is incomplete: ${FOUNDATION_MISSING[*]}"
    die "Run 'bash build_rg.sh' ONCE first to create the resource group + platform (non-billable), then re-run build.sh."
  fi
  ok "Foundation present: RG + ACR + UAMI + Container Apps env."
  regen_phase1_outputs
}

ok "env.sh sourced (project=$PROJECT_TAG_VALUE, suffix=$SUFFIX)"

print_demo_urls() {
  # Print URLs discovered from phase outputs. Safe when some phases have not run.
  local root
  root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  local toolapi="" dash="" search="" videoassist=""
  [[ -f "$root/phase4-toolapi/outputs.env" ]] && source "$root/phase4-toolapi/outputs.env" && toolapi="${TOOLAPI_URL:-}"
  [[ -f "$root/phase6-crm/outputs.env" ]] && source "$root/phase6-crm/outputs.env" && dash="${DASH_URL:-${DASHBOARD_URL:-}}"
  [[ -f "$root/phase9-videoassist/outputs.env" ]] && source "$root/phase9-videoassist/outputs.env" && videoassist="${VIDEOASSIST_URL:-}"
  [[ -f "$root/phase5-rag/outputs.env" ]] && source "$root/phase5-rag/outputs.env" && search="${SEARCH_ENDPOINT:-${RAG_INDEX_NAME:-${search:-}}}"
  cat <<EOF

$(printf '\033[1;36m========== Contoso Retail RM Assist — Rakesh Sharma ==========\033[0m')
  CRM Dashboard:             ${dash:-not deployed yet}
  Video Assist (Step 7):     ${videoassist:-not deployed yet}
  Step 7 launch pattern:     ${videoassist:+$videoassist/?customer_id=$DEFAULT_CUSTOMER_ID}
  Tool API:                  ${toolapi:-not deployed yet}
  AI Search endpoint:        ${search:-see phase5 outputs / Azure portal}

  Health checks:
    ${toolapi:+curl -fsS "$toolapi/healthz"}
    ${dash:+curl -fsS "$dash/healthz"}
    ${videoassist:+curl -fsS "$videoassist/healthz"}
$(printf '\033[1;36m==============================================================\033[0m')
EOF
}
