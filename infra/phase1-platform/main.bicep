// infra/phase1-platform/main.bicep
// Phase 1 — Platform substrate (Contoso Retail RM Assist)
//
// Creates: Log Analytics and the User-Assigned Managed Identity. That is all that is left.
//
// The ACR, the Container Apps Environment and their AcrPull/AcrPush role assignments were
// removed when the three Container Apps became native systemd services on the phase10 VM.
// Nothing is containerised any more, so there is no registry to push to and no environment
// to host apps in.
//
// THE UAMI IS LOAD-BEARING — do not be tempted to remove it as "unused here". Every phase2
// role assignment (AI Foundry, AI Search, ACS) is keyed to its principalId, and phase10
// attaches it to the VM as a user-assigned identity so the app services inherit exactly
// those grants. Re-keying to a VM principal would be circular, because phase2 runs before
// the VM exists.
//
// This stack is Key Vault-free: app secrets reach the VM through a root-owned 0600 systemd
// EnvironmentFile written by phase10, never through a Key Vault or a container secret.
//
// All resources tagged project=... so down.sh can verify ownership before deleting.

targetScope = 'resourceGroup'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Project tag value applied to every resource.')
param projectTag string = 'contoso-retail-rm-assist-rakesh'

// ----- Names (defaults derived from env.sh; overridable) -----
@description('Log Analytics workspace name.')
param lawName  string = 'log-rmx'
@description('User-assigned managed identity name.')
param uamiName string = 'id-rmx-app'

var commonTags = {
  project: projectTag
  phase: 'phase1'
}

resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: lawName
  location: location
  tags: commonTags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    features: { enableLogAccessUsingOnlyResourcePermissions: true }
  }
}

resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: uamiName
  location: location
  tags: commonTags
}

output lawId            string = law.id
output lawCustomerId    string = law.properties.customerId
output uamiId           string = uami.id
output uamiPrincipalId  string = uami.properties.principalId
output uamiClientId     string = uami.properties.clientId
