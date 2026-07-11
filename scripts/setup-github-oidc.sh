#!/usr/bin/env bash
# =============================================================================
# setup-github-oidc.sh — one-shot: let GitHub Actions deploy this repo to Azure
# WITHOUT storing any password, using OpenID Connect (OIDC) federated identity.
#
# What it does (all via `az` + `gh`, no portal clicks):
#   1. Creates/reuses an Entra app registration + service principal.
#   2. Adds a GitHub OIDC *federated credential* trusting this repo's `main`
#      branch (issuer token.actions.githubusercontent.com).
#   3. Grants that identity the "Owner" role on the target subscription so
#      build.sh can create resources AND assign roles (phase2 does role grants).
#   4. Sets the repository secrets the workflows need:
#        AZURE_CLIENT_ID  AZURE_TENANT_ID  AZURE_SUBSCRIPTION_ID
#      and, if present in your shell, the optional integration secrets
#      (TEAMS_WEBHOOK_URL, GRAPH_*, RM_USER_ID, RM_MEETING_URL, SCHEDULE_WEBHOOK_URL).
#
# PREREQUISITES:
#   * az  logged in as a subscription Owner / User Access Administrator:  az login
#   * gh  logged in with repo admin rights:                              gh auth login
#   * You can create app registrations in the tenant (default tenant policy allows it).
#
# USAGE (from the repo root):
#   bash scripts/setup-github-oidc.sh
#   # or target a specific repo / branch explicitly:
#   GH_REPO=hrshl-demo/contoso-retail-acs-rm-assist-videocall BRANCH=main \
#     bash scripts/setup-github-oidc.sh
# =============================================================================
set -euo pipefail

c(){ printf '\033[%sm%s\033[0m' "$1" "$2"; }
ok(){   printf '  %s %s\n' "$(c '1;32' '✓')" "$1"; }
info(){ printf '  %s %s\n' "$(c '1;36' '•')" "$1"; }
die(){  printf '%s %s\n' "$(c '1;31' '✗ ERROR:')" "$1" >&2; exit 1; }
step(){ printf '\n%s\n' "$(c '1;37' "▶ $1")"; }

BRANCH="${BRANCH:-main}"

step "Preflight"
command -v az >/dev/null 2>&1 || die "Azure CLI (az) not found."
command -v gh >/dev/null 2>&1 || die "GitHub CLI (gh) not found. Install it and run 'gh auth login'."
az account show >/dev/null 2>&1 || die "Not logged in to Azure. Run: az login"
gh auth status >/dev/null 2>&1 || die "Not logged in to GitHub. Run: gh auth login"

# Resolve owner/repo: explicit GH_REPO wins, else ask gh about the local repo.
REPO="${GH_REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)}"
[[ -n "$REPO" ]] || die "Could not determine the repo. Set GH_REPO=owner/repo and re-run."
SUB_ID="$(az account show --query id -o tsv)"
TENANT_ID="$(az account show --query tenantId -o tsv)"
APP_NAME="${OIDC_APP_NAME:-gh-oidc-${REPO##*/}}"
SUBJECT="repo:${REPO}:ref:refs/heads/${BRANCH}"
ok "Repo        $REPO"
ok "Subscription $SUB_ID"
ok "Tenant       $TENANT_ID"
ok "OIDC subject $SUBJECT"

step "App registration '$APP_NAME'"
APP_ID="$(az ad app list --display-name "$APP_NAME" --query "[0].appId" -o tsv 2>/dev/null || true)"
if [[ -z "$APP_ID" || "$APP_ID" == "null" ]]; then
  APP_ID="$(az ad app create --display-name "$APP_NAME" --sign-in-audience AzureADMyOrg --query appId -o tsv)"
  ok "Created app $APP_ID"
else
  ok "Reusing app $APP_ID"
fi
if ! az ad sp show --id "$APP_ID" >/dev/null 2>&1; then
  az ad sp create --id "$APP_ID" >/dev/null; ok "Created service principal"
else
  info "Service principal already exists"
fi
SP_OID="$(az ad sp show --id "$APP_ID" --query id -o tsv)"

step "GitHub OIDC federated credential"
FED_NAME="gh-${BRANCH}"
EXISTING_SUBJECT="$(az ad app federated-credential list --id "$APP_ID" \
  --query "[?name=='$FED_NAME'].subject | [0]" -o tsv 2>/dev/null || true)"
if [[ "$EXISTING_SUBJECT" == "$SUBJECT" ]]; then
  info "Federated credential '$FED_NAME' already trusts $SUBJECT"
else
  [[ -n "$EXISTING_SUBJECT" ]] && az ad app federated-credential delete --id "$APP_ID" --federated-credential-id "$FED_NAME" >/dev/null 2>&1 || true
  az ad app federated-credential create --id "$APP_ID" --parameters "{
    \"name\": \"$FED_NAME\",
    \"issuer\": \"https://token.actions.githubusercontent.com\",
    \"subject\": \"$SUBJECT\",
    \"audiences\": [\"api://AzureADTokenExchange\"]
  }" >/dev/null
  ok "Trusts GitHub Actions on ${REPO}@${BRANCH}"
fi

step "Role assignment (Owner on the subscription)"
if az role assignment list --assignee "$APP_ID" --scope "/subscriptions/$SUB_ID" \
     --query "[?roleDefinitionName=='Owner'] | [0]" -o tsv 2>/dev/null | grep -q .; then
  info "Owner already assigned"
else
  az role assignment create --assignee-object-id "$SP_OID" --assignee-principal-type ServicePrincipal \
    --role "Owner" --scope "/subscriptions/$SUB_ID" >/dev/null
  ok "Granted Owner at subscription scope"
fi

step "Setting GitHub repository secrets"
gh secret set AZURE_CLIENT_ID       -R "$REPO" -b "$APP_ID"    >/dev/null && ok "AZURE_CLIENT_ID"
gh secret set AZURE_TENANT_ID       -R "$REPO" -b "$TENANT_ID" >/dev/null && ok "AZURE_TENANT_ID"
gh secret set AZURE_SUBSCRIPTION_ID -R "$REPO" -b "$SUB_ID"    >/dev/null && ok "AZURE_SUBSCRIPTION_ID"

# Optional integration secrets — only pushed if present in your shell.
for v in TEAMS_WEBHOOK_URL GRAPH_TENANT_ID GRAPH_CLIENT_ID GRAPH_CLIENT_SECRET RM_USER_ID RM_MEETING_URL SCHEDULE_WEBHOOK_URL; do
  val="${!v:-}"
  [[ -n "$val" ]] && gh secret set "$v" -R "$REPO" -b "$val" >/dev/null && ok "$v (optional)"
done

step "Done"
cat <<EOF

  GitHub Actions can now deploy to Azure with no stored password.

  Next:
    • Actions ▸ "Deploy to Azure" ▸ Run workflow ▸ choose ptu / payg
    • Actions ▸ "Wipe Azure"      ▸ Run workflow ▸ keep-rg / delete-rg

  Calendared Teams meetings still need a ONE-TIME admin step (Graph consent):
    run 'bash setup-graph.sh' once from your VM/Cloud Shell with a Global
    Administrator activated — see docs/ENTRA_PIM_ADMIN.md — then add its
    GRAPH_* / RM_USER_ID values as repo secrets (re-run this script with them
    exported, or 'gh secret set').
EOF
