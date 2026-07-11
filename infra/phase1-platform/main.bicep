// infra/phase1-platform/main.bicep
// Phase 1 — Platform substrate (Contoso MSME RM Assist POC)
//
// Creates: Log Analytics, ACR (Basic, admin disabled), User-Assigned Managed
// Identity, Container Apps Environment, and role assignments:
//   UAMI -> ACR AcrPull ; Deployer -> ACR AcrPush (for `az acr build`).
// NOTE: This stack is Key Vault-free. All app secrets are passed as literal
// Container App secrets at deploy time (see phase4/phase6), so the build is
// immune to subscription policies that lock down Key Vault public access.
//
// All resources tagged project=contoso-msme-rm-assist so down.sh can verify ownership.

targetScope = 'resourceGroup'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Deterministic suffix for globally-unique names.')
param suffix string

@description('Object ID of the principal running this deployment (you).')
param deployerObjectId string

@description('Principal type of the deployer.')
@allowed([ 'User', 'ServicePrincipal', 'Group' ])
param deployerPrincipalType string = 'User'

@description('Project tag value applied to every resource.')
param projectTag string = 'contoso-retail-rm-assist-rakesh'

// ----- Names (defaults derived from suffix; overridable from infra/common/env.sh) -----
@description('Log Analytics workspace name.')
param lawName  string = 'log-rmx'
@description('Container Registry name (globally unique).')
param acrName  string = 'acrrmx${suffix}'
@description('User-assigned managed identity name.')
param uamiName string = 'id-rmx-app'
@description('Container Apps environment name.')
param caeName  string = 'cae-rmx'

// ----- Built-in role definition IDs (stable across Azure) -----
var roleIds = {
  acrPull:               '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  acrPush:               '8311e382-0749-4cb8-b61a-304f252e45ec'
}

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

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: acrName
  location: location
  tags: commonTags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
    anonymousPullEnabled: false
    zoneRedundancy: 'Disabled'
  }
}

resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: uamiName
  location: location
  tags: commonTags
}

resource cae 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: caeName
  location: location
  tags: commonTags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: law.properties.customerId
        sharedKey: law.listKeys().primarySharedKey
      }
    }
    zoneRedundant: false
    workloadProfiles: [
      { name: 'Consumption', workloadProfileType: 'Consumption' }
    ]
  }
}

resource ra_uami_acrpull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, uami.id, roleIds.acrPull)
  scope: acr
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.acrPull)
  }
}

resource ra_deployer_acrpush 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, deployerObjectId, roleIds.acrPush)
  scope: acr
  properties: {
    principalId: deployerObjectId
    principalType: deployerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.acrPush)
  }
}

output lawId            string = law.id
output lawCustomerId    string = law.properties.customerId
output acrId            string = acr.id
output acrLoginServer   string = acr.properties.loginServer
output uamiId           string = uami.id
output uamiPrincipalId  string = uami.properties.principalId
output uamiClientId     string = uami.properties.clientId
output caeId            string = cae.id
output caeDefaultDomain string = cae.properties.defaultDomain
