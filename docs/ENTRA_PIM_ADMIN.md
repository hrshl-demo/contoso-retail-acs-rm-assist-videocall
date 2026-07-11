# Entra: temporary Global Admin + automated Graph consent

This demo can create a **real Teams meeting and a calendar event on the RM's
calendar** for every "Video call your RM" tap. That uses **Microsoft Graph
application permissions** (`Calendars.ReadWrite`), and application permissions
**must be admin-consented** — that is Entra's security model, not a limitation of
this project. `setup-graph.sh` automates *everything except* the one thing only a
directory admin can do: **grant the consent**. This page explains how to get that
privilege temporarily and safely.

> If you don't need the calendared flow, you can skip all of this. The video call
> still works with a standing meeting link (`RM_MEETING_URL`) or a Power Automate
> scheduling flow (`SCHEDULE_WEBHOOK_URL`). See [POWER_AUTOMATE.md](POWER_AUTOMATE.md).

---

## Two different "admin" planes (this trips everyone up)

| Plane | Governs | Typical roles | Lets you grant Graph consent? |
|-------|---------|---------------|-------------------------------|
| **Azure RBAC** | Subscriptions, resource groups, resources | Owner, Contributor, User Access Administrator | **No** |
| **Entra directory roles** | App registrations, admin consent, users, groups | Global Administrator, Privileged Role Administrator, Cloud/Application Administrator | **Yes** (GA or Privileged Role Admin) |

You can be **Owner of the whole subscription** and still be unable to grant admin
consent, because that lives on the *directory* plane. Creating the app
registration usually works anyway (most tenants have
`defaultUserRolePermissions.allowedToCreateApps = true`), but the **consent** step
needs a directory admin.

Check what you currently hold:

```bash
# Your active directory roles (e.g. "Global Reader" = read-only, cannot consent)
az rest --method GET \
  --url "https://graph.microsoft.com/v1.0/me/transitiveMemberOf/microsoft.graph.directoryRole?\$select=displayName" \
  --query "value[].displayName" -o table

# Your Azure RBAC (Owner etc. — does NOT help with consent)
az role assignment list --assignee "$(az ad signed-in-user show --query id -o tsv)" --all \
  --query "[].roleDefinitionName" -o table
```

---

## Get Global Administrator *temporarily* with PIM (recommended)

Privileged Identity Management (PIM) lets you **activate** an admin role for a few
hours instead of holding it permanently — the least-privilege way to do a one-off
consent.

1. Open **[Microsoft Entra admin center → PIM → My roles → Eligible assignments](https://entra.microsoft.com/#view/Microsoft_Azure_PIMCommon/ResourceMenuBlade/~/aadmigratedroles)**.
2. If **Global Administrator** (or **Privileged Role Administrator**) appears with an **Activate** link, click **Activate**.
3. Set a short duration (e.g. 1–2 hours), complete MFA, add a justification, confirm.
4. Wait ~2–5 minutes for activation to propagate.

> **No eligible assignment?** Someone with User Access / Privileged Role
> Administrator must make you *eligible* first (PIM → Roles → Global Administrator
> → Add assignments → Eligible). Or ask an existing admin to run the consent step
> — they can run `setup-graph.sh` for you, or grant consent for the app in the
> portal after you've created it.

---

## Run the one-shot Graph setup

With the admin role active, from the repo root **on a machine that is `az`
logged in to your tenant** (your Azure VM or Azure Cloud Shell):

```bash
RM_UPN=admin@MngEnvMCAP175622.onmicrosoft.com bash setup-graph.sh
```

`RM_UPN` is the **RM's real, Exchange-Online-licensed mailbox** — the calendar the
meetings land on. The script will:

1. Create/reuse the app registration `contoso-videoassist-rm-calendar`.
2. Declare the `Calendars.ReadWrite` **application** permission.
3. **Grant tenant admin consent** (this is the step that needs your active admin role).
4. Mint a client secret.
5. Write `infra/common/secrets.env` (git-ignored) with
   `GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET / RM_USER_ID`.

`env.sh` sources `secrets.env` automatically, so the next `bash build.sh` deploys
the calendared flow with zero further steps.

### Headless VM note
Your VM may not be able to open a browser (`gio: Operation not supported`). If you
are **already** `az login`'d (check `az account show`), you don't need to log in
again. If you must, use device-code flow: `az login --use-device-code`.

---

## Feed the credentials to GitHub Actions

CI/CD deploys run on a GitHub runner that has **no** `secrets.env`. Push the four
Graph values as repository secrets so the deploy workflow can rebuild the file:

```bash
gh secret set GRAPH_TENANT_ID     -R hrshl-demo/contoso-retail-acs-rm-assist-videocall -b "<tenant-id>"
gh secret set GRAPH_CLIENT_ID     -R hrshl-demo/contoso-retail-acs-rm-assist-videocall -b "<client-id>"
gh secret set GRAPH_CLIENT_SECRET -R hrshl-demo/contoso-retail-acs-rm-assist-videocall -b "<client-secret>"
gh secret set RM_USER_ID          -R hrshl-demo/contoso-retail-acs-rm-assist-videocall -b "<rm-object-id>"
```

(Or `source infra/common/secrets.env` and re-run `scripts/setup-github-oidc.sh` —
it pushes any `GRAPH_*` it finds in your shell.)

---

## Clean up

- **Deactivate the admin role** when you're done: PIM → My roles → Active
  assignments → Deactivate. (It also auto-expires.)
- The app registration is deleted by a full teardown from an admin machine:
  `bash wipe.sh --delete-rg` (`WIPE_GRAPH_APP=1`, the default). The GitHub *Wipe*
  workflow sets `WIPE_GRAPH_APP=0` because the runner can't touch directory
  objects — remove the app from an admin machine, or:
  ```bash
  az ad app delete --id "$(az ad app list --display-name contoso-videoassist-rm-calendar --query '[0].appId' -o tsv)"
  ```

## Production note

In production you would **not** activate GA per deploy. Instead, one directory
admin grants consent **once** to the app; from then on it runs unattended with its
own identity (client credentials). The calendar events land on a **service /
scheduling mailbox** (e.g. `rm-bookings@bank.com`) rather than a person, and the
client secret is replaced by a **certificate** or **workload-identity federation**
and rotated automatically. The customer never sees any of this — they only ever
get a "Join call" button.
