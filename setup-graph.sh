#!/usr/bin/env bash
# =============================================================================
# setup-graph.sh — ONE-SHOT, FULLY-AUTOMATED provisioning of the Microsoft Graph
# app registration used to create a REAL Teams meeting + a calendar event on the
# RM's calendar for every customer "Video call your RM" tap.
#
# It creates/updates (all via `az`, no portal clicks):
#   1. An Entra app registration + service principal
#   2. The Microsoft Graph **Calendars.ReadWrite (Application)** permission
#   3. Tenant admin consent for that permission (app-role assignment)
#   4. A client secret
# then writes GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET / RM_USER_ID
# into infra/common/secrets.env, which env.sh sources automatically — so the next
# `bash build.sh` deploys the calendared, fully-automated flow with zero manual steps.
#
# Idempotent: safe to re-run (reuses the app, rotates the secret).
#
# ---------------------------------------------------------------------------
# PREREQUISITES (the ONLY thing you must provide):
#   * Azure CLI logged in:  az login   (add --tenant <id> if you have several)
#   * The signed-in identity must be able to (a) create app registrations
#     [Application Administrator or Cloud Application Administrator] and
#     (b) grant tenant admin consent [Privileged Role Administrator or Global
#     Administrator]. Admin consent for an application permission ALWAYS needs an
#     admin — that is Entra's security model — but this script performs it for you.
#   * RM_UPN = the RM's real mailbox (Exchange Online licensed), e.g.
#     priya.nair@contoso.com — this is the calendar the meetings land on.
#
# USAGE:
#   RM_UPN=priya.nair@contoso.com bash setup-graph.sh
#   # or:  bash setup-graph.sh priya.nair@contoso.com
# =============================================================================
set -euo pipefail

# ---- pretty output -------------------------------------------------------
c(){ printf '\033[%sm%s\033[0m' "$1" "$2"; }
ok(){   printf '  %s %s\n' "$(c '1;32' '✓')" "$1"; }
info(){ printf '  %s %s\n' "$(c '1;36' '•')" "$1"; }
warn(){ printf '  %s %s\n' "$(c '1;33' '!')" "$1"; }
die(){  printf '%s %s\n' "$(c '1;31' '✗ ERROR:')" "$1" >&2; exit 1; }
step(){ printf '\n%s\n' "$(c '1;37' "▶ $1")"; }

APP_NAME="${GRAPH_APP_NAME:-contoso-videoassist-rm-calendar}"
RM_UPN="${RM_UPN:-${1:-}}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECRETS_FILE="$SCRIPT_DIR/infra/common/secrets.env"
GRAPH_APP_ID="00000003-0000-0000-c000-000000000000"      # Microsoft Graph (well-known)
SECRET_YEARS="${GRAPH_SECRET_YEARS:-1}"

# ---- preflight -----------------------------------------------------------
step "Preflight"
command -v az >/dev/null 2>&1 || die "Azure CLI (az) not found. Install it and run 'az login'."
ACCOUNT_JSON="$(az account show 2>/dev/null)" || die "Not logged in. Run: az login"
TENANT_ID="$(az account show --query tenantId -o tsv)"
SIGNED_IN="$(az account show --query user.name -o tsv)"
ok "Signed in as $SIGNED_IN"
ok "Tenant $TENANT_ID"
[[ -n "$RM_UPN" ]] || die "RM_UPN not set. Pass the RM mailbox, e.g.: RM_UPN=priya.nair@contoso.com bash setup-graph.sh"

# ---- resolve the RM mailbox ---------------------------------------------
step "Resolving RM mailbox ($RM_UPN)"
RM_OBJECT_ID="$(az ad user show --id "$RM_UPN" --query id -o tsv 2>/dev/null)" \
  || die "Could not find user '$RM_UPN' in tenant $TENANT_ID. Use the RM's exact UPN/email."
ok "RM object id $RM_OBJECT_ID"

# ---- create / reuse the app registration --------------------------------
step "App registration '$APP_NAME'"
APP_ID="$(az ad app list --display-name "$APP_NAME" --query "[0].appId" -o tsv 2>/dev/null || true)"
if [[ -z "$APP_ID" || "$APP_ID" == "null" ]]; then
  APP_ID="$(az ad app create --display-name "$APP_NAME" --sign-in-audience AzureADMyOrg --query appId -o tsv)"
  ok "Created app $APP_ID"
else
  ok "Reusing existing app $APP_ID"
fi

# ensure a service principal exists for the app
if ! az ad sp show --id "$APP_ID" >/dev/null 2>&1; then
  az ad sp create --id "$APP_ID" >/dev/null
  ok "Created service principal"
else
  info "Service principal already exists"
fi
APP_SP_ID="$(az ad sp show --id "$APP_ID" --query id -o tsv)"

# ---- resolve Graph SP + the Calendars.ReadWrite app-role id -------------
step "Microsoft Graph permission (Calendars.ReadWrite, Application)"
GRAPH_SP_ID="$(az ad sp show --id "$GRAPH_APP_ID" --query id -o tsv)"
ROLE_ID="$(az ad sp show --id "$GRAPH_APP_ID" \
  --query "appRoles[?value=='Calendars.ReadWrite' && contains(allowedMemberTypes,'Application')].id | [0]" -o tsv)"
[[ -n "$ROLE_ID" && "$ROLE_ID" != "null" ]] || die "Could not resolve the Calendars.ReadWrite app-role id from Graph."
ok "Role id $ROLE_ID"

# declare the permission on the app manifest (for visibility in the portal)
az ad app permission add --id "$APP_ID" --api "$GRAPH_APP_ID" --api-permissions "$ROLE_ID=Role" >/dev/null 2>&1 || true
ok "Permission declared on the app"

# ---- grant tenant admin consent (app-role assignment) -------------------
step "Granting tenant admin consent"
# The app SP can take a few seconds to propagate before it can be assigned a role.
CONSENTED=""
CONSENT_ERR="$(mktemp 2>/dev/null || echo "${TMPDIR:-/tmp}/consent.$$.err")"
for attempt in 1 2 3 4 5 6; do
  if az rest --method POST \
      --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$APP_SP_ID/appRoleAssignments" \
      --headers "Content-Type=application/json" \
      --body "{\"principalId\":\"$APP_SP_ID\",\"resourceId\":\"$GRAPH_SP_ID\",\"appRoleId\":\"$ROLE_ID\"}" \
      >/dev/null 2>"$CONSENT_ERR"; then
    CONSENTED="yes"; break
  fi
  if grep -qiE "already exists|Permission being assigned already exists" "$CONSENT_ERR" 2>/dev/null; then
    CONSENTED="already"; break
  fi
  if grep -qiE "Authorization_RequestDenied|Insufficient privileges" "$CONSENT_ERR" 2>/dev/null; then
    rm -f "$CONSENT_ERR"
    die "Admin consent denied — the signed-in account lacks privilege. Re-run 'az login' as a Global Administrator or Privileged Role Administrator, then run this script again."
  fi
  info "Waiting for the service principal to propagate (attempt $attempt/6)…"
  sleep 10
done
rm -f "$CONSENT_ERR"
[[ -n "$CONSENTED" ]] || die "Could not grant admin consent after several attempts. Re-run the script; if it persists, confirm your account can grant consent."
[[ "$CONSENTED" == "already" ]] && info "Consent already in place" || ok "Admin consent granted"

# ---- mint a fresh client secret -----------------------------------------
step "Client secret"
CLIENT_SECRET="$(az ad app credential reset --id "$APP_ID" \
  --display-name "videoassist-graph" --years "$SECRET_YEARS" \
  --query password -o tsv)"
[[ -n "$CLIENT_SECRET" ]] || die "Failed to create a client secret."
ok "Secret minted (valid ${SECRET_YEARS}y) — stored only in secrets.env, shown once by az"

# ---- write secrets.env ---------------------------------------------------
step "Writing $SECRETS_FILE"
mkdir -p "$(dirname "$SECRETS_FILE")"
umask 077
cat > "$SECRETS_FILE" <<EOF
# Auto-generated by setup-graph.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ) — DO NOT COMMIT.
# Sourced automatically by infra/common/env.sh. Re-run setup-graph.sh to rotate.
export GRAPH_TENANT_ID="$TENANT_ID"
export GRAPH_CLIENT_ID="$APP_ID"
export GRAPH_CLIENT_SECRET="$CLIENT_SECRET"
export RM_USER_ID="$RM_OBJECT_ID"
export MEETING_TIMEZONE="\${MEETING_TIMEZONE:-India Standard Time}"
export MEETING_DURATION_MINUTES="\${MEETING_DURATION_MINUTES:-30}"
EOF
chmod 600 "$SECRETS_FILE" 2>/dev/null || true
ok "Wrote credentials (file permissions 600)"

# ---- done ----------------------------------------------------------------
step "Done — fully automated, calendared Teams meetings are configured"
cat <<EOF

  App name       : $APP_NAME
  App (client) id: $APP_ID
  Tenant id      : $TENANT_ID
  RM mailbox     : $RM_UPN  ($RM_OBJECT_ID)
  Permission     : Microsoft Graph · Calendars.ReadWrite (Application) · admin-consented
  Secret         : written to infra/common/secrets.env (git-ignored)

  Next:
    bash build_rg.sh            # once, if you haven't already
    bash build.sh --type=ptu    # or --type=payg  (env.sh now picks up the Graph creds)

  From then on: every customer tap on "Video call your RM" creates a real Teams
  meeting on $RM_UPN's calendar and both sides join the SAME meeting — no manual steps.

EOF
