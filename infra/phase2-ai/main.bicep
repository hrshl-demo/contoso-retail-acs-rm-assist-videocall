// infra/phase2-ai/main.bicep
// Phase 2 — AI services (SELF-CONTAINED build; nothing pre-existing)
//
// Creates:
//   - Azure AI Foundry account (Microsoft.CognitiveServices/accounts, kind=AIServices,
//     custom subdomain, system-assigned identity, project management enabled)
//   - Azure AI Foundry PROJECT (child of the account)
//   - Azure AI Search (Basic, Entra-only auth, public network)
//   - Azure Communication Services (ACS)
//   - Speech (Cognitive Services) account for the ACS media-streaming fallback
//   - Role assignments:
//       * UAMI -> NEW AIServices account: Cognitive Services User + Cognitive Services OpenAI User
//       * UAMI -> NEW Foundry project:    Azure AI User
//       * UAMI -> NEW Search:  Search Index Data Contributor + Search Service Contributor
//       * UAMI -> NEW ACS:     Contributor (ACS management for token/identity operations)
//       * Deployer (you) -> NEW AIServices: Cognitive Services OpenAI User + Azure AI User
//                          (so you can create model deployments + run the indexer locally)
//       * Deployer (you) -> NEW Search: Index Data Contributor + Service Contributor
//
// The model DEPLOYMENTS (chat + embeddings) are created by phase2-ai/up.sh via `az cli`
// AFTER this Bicep provisions the account, so their SKU/version can be resolved dynamically.

targetScope = 'resourceGroup'

// ====================== Parameters ======================
@description('Azure region.')
param location string = resourceGroup().location

@description('Region for AI Search (can differ from main region if main region is capacity-constrained).')
param searchLocation string = location

@description('Region for the standalone Speech (SpeechServices) account. southindia does not offer the standalone SpeechServices kind, so this defaults independently (e.g. centralindia).')
param speechLocation string = location

@description('Deterministic suffix for globally-unique names.')
param suffix string

@description('Object ID of the deployer.')
param deployerObjectId string

@description('Deployer principal type.')
@allowed([ 'User', 'ServicePrincipal', 'Group' ])
param deployerPrincipalType string = 'User'

@description('Project tag value.')
param projectTag string = 'contoso-retail-rm-assist-rakesh'

// ----- AI Foundry (CREATED here) -----
@description('Name of the AIServices account (kind=AIServices) to CREATE. Also its custom subdomain, so it must be globally unique.')
param aiServicesName string

@description('Name of the AI Foundry project to CREATE inside the AIServices account.')
param foundryProjectName string

@description('Voice Live managed model identifier (NOT a deployment). E.g. gpt-4.1, gpt-realtime.')
param voiceLiveModel string

// ----- Chat/embed deployment NAMES (deployments themselves are created by up.sh) -----
@description('Chat model deployment name that up.sh will create on the account (for KV secret wiring).')
param chatDeploymentName string

@description('Embedding model deployment name that up.sh will create on the account (for KV secret wiring).')
param embedDeploymentName string

// ----- Phase 1 outputs needed here -----
@description('Principal ID of the UAMI created in Phase 1.')
param uamiPrincipalId string

// ----- ACS data residency -----
@description('Data location (residency) for Azure Communication Services.')
@allowed([ 'Africa', 'Asia Pacific', 'Australia', 'Brazil', 'Canada', 'Europe', 'France', 'Germany', 'India', 'Japan', 'Korea', 'Norway', 'Switzerland', 'UAE', 'UK', 'United States' ])
param acsDataLocation string = 'India'

// ====================== Names (defaults from suffix; overridable from infra/common/env.sh) ======================
@description('AI Search service name (globally unique).')
param searchName string = 'srch-rmx-${suffix}'
@description('Communication Services (ACS) name.')
param acsName    string = 'acs-rmx-${suffix}'
@description('Speech (Cognitive Services) account for the ACS media-streaming fallback.')
param speechName string = 'spch-rmx-${suffix}'

// ====================== Role definition IDs (built-in) ======================
var roleIds = {
  cognitiveServicesUser:        'a97b65f3-24c7-4388-baec-2e87135dc908'
  cognitiveServicesOpenAIUser:  '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
  azureAIUser:                  '53ca6127-db72-4b80-b1b0-d745d6d5456d'  // Azure AI User (Foundry project)
  searchIndexDataContributor:   '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
  searchServiceContributor:     '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
  contributor:                  'b24988ac-6180-42a0-ab88-20f7382dd24c'
}

var commonTags = {
  project: projectTag
  phase: 'phase2'
}

// ====================== NEW: AI Foundry (AIServices) account + project ======================
resource aiServices 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: aiServicesName
  location: location
  tags: commonTags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    // Custom subdomain is REQUIRED for token (Entra) auth against the OpenAI data plane.
    customSubDomainName: aiServicesName
    publicNetworkAccess: 'Enabled'
    // Enable Foundry project management so the child project resource is supported.
    allowProjectManagement: true
    disableLocalAuth: false
  }
}

resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: aiServices
  name: foundryProjectName
  location: location
  tags: commonTags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {}
}

// ====================== NEW: AI Search ======================
resource search 'Microsoft.Search/searchServices@2024-06-01-preview' = {
  name: searchName
  location: searchLocation
  tags: commonTags
  sku: {
    name: 'basic'
  }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'default'
    publicNetworkAccess: 'enabled'
    disableLocalAuth: true                  // <-- Entra only; admin/query keys disabled
    authOptions: null
    semanticSearch: 'free'                  // Basic SKU includes free semantic ranking quota
    networkRuleSet: {
      ipRules: []
      bypass: 'AzureServices'
    }
  }
  identity: {
    type: 'SystemAssigned'
  }
}


// ====================== NEW: Speech service for media-streaming fallback ======================
// Native ACS real-time transcription is still attempted first. This SpeechServices
// account is used only by the POC fallback path: ACS audio streaming -> Azure Speech SDK.
resource speech 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: speechName
  location: speechLocation
  tags: union(commonTags, {
    purpose: 'acs-media-speech-fallback'
  })
  kind: 'SpeechServices'
  sku: {
    name: 'S0'
  }
  properties: {
    publicNetworkAccess: 'Enabled'
    customSubDomainName: speechName
  }
}

// ====================== NEW: ACS ======================
// This demo sends NO email, so no Email Communication Service / sender domain is
// provisioned. ACS is created stand-alone (video/voice tokens + interop only).

resource acs 'Microsoft.Communication/communicationServices@2023-04-01' = {
  name: acsName
  location: 'global'                        // ACS top-level is always 'global'
  tags: commonTags
  identity: {
    type: 'SystemAssigned'                  // needed so grant-acs-cognitive-role.sh can grant
  }                                          // this ACS's identity Cognitive Services User (transcription)
  properties: {
    dataLocation: acsDataLocation
  }
}

// ====================== Role assignments ======================

// UAMI -> NEW AIServices: Cognitive Services User
resource ra_uami_cogsvcs 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(aiServices.id, uamiPrincipalId, roleIds.cognitiveServicesUser)
  scope: aiServices
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.cognitiveServicesUser)
  }
}

// UAMI -> NEW AIServices: Cognitive Services OpenAI User (covers /openai/v1/* path)
resource ra_uami_aoaiuser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(aiServices.id, uamiPrincipalId, roleIds.cognitiveServicesOpenAIUser)
  scope: aiServices
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.cognitiveServicesOpenAIUser)
  }
}

// UAMI -> NEW Speech account: Cognitive Services User (mint short-lived Speech tokens for the in-call STT)
resource ra_uami_speech 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(speech.id, uamiPrincipalId, roleIds.cognitiveServicesUser)
  scope: speech
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.cognitiveServicesUser)
  }
}

// UAMI -> NEW Foundry project: Azure AI User
resource ra_uami_aiuser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryProject.id, uamiPrincipalId, roleIds.azureAIUser)
  scope: foundryProject
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.azureAIUser)
  }
}

// Deployer (you) -> NEW AIServices: Cognitive Services OpenAI User (call the model at runtime/testing)
resource ra_deployer_aoaiuser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(aiServices.id, deployerObjectId, roleIds.cognitiveServicesOpenAIUser)
  scope: aiServices
  properties: {
    principalId: deployerObjectId
    principalType: deployerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.cognitiveServicesOpenAIUser)
  }
}

// Deployer (you) -> NEW Foundry project: Azure AI User
resource ra_deployer_aiuser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryProject.id, deployerObjectId, roleIds.azureAIUser)
  scope: foundryProject
  properties: {
    principalId: deployerObjectId
    principalType: deployerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.azureAIUser)
  }
}

// UAMI -> NEW Search: Index Data Contributor
resource ra_uami_searchdata 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, uamiPrincipalId, roleIds.searchIndexDataContributor)
  scope: search
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.searchIndexDataContributor)
  }
}

// UAMI -> NEW Search: Service Contributor (create/update index schema)
resource ra_uami_searchsvc 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, uamiPrincipalId, roleIds.searchServiceContributor)
  scope: search
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.searchServiceContributor)
  }
}

// UAMI -> NEW ACS: Contributor (ACS management for token/identity operations)
resource ra_uami_acs 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acs.id, uamiPrincipalId, roleIds.contributor)
  scope: acs
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.contributor)
  }
}

// Deployer (you) -> NEW Search: Index Data Contributor + Service Contributor (run indexer locally in Phase 5)
resource ra_deployer_searchdata 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, deployerObjectId, roleIds.searchIndexDataContributor)
  scope: search
  properties: {
    principalId: deployerObjectId
    principalType: deployerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.searchIndexDataContributor)
  }
}

resource ra_deployer_searchsvc 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, deployerObjectId, roleIds.searchServiceContributor)
  scope: search
  properties: {
    principalId: deployerObjectId
    principalType: deployerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.searchServiceContributor)
  }
}

// ====================== Outputs ======================
// NOTE: This stack is Key Vault-free. Foundry/Search/ACS wiring values are emitted as
// deployment outputs (below) and persisted to phase2 outputs.env by up.sh, then passed
// to the Tool API / dashboard as literal Container App secrets at deploy time.
output aiServicesName             string = aiServices.name
output aiServicesId               string = aiServices.id
output foundryProjectName         string = foundryProject.name
output foundryProjectId           string = foundryProject.id
output searchName                 string = search.name
output searchEndpoint             string = 'https://${searchName}.search.windows.net/'
output acsName                    string = acs.name
output speechName                 string = speech.name
output speechEndpoint             string = 'https://${speechName}.cognitiveservices.azure.com/'
output speechRegion               string = speechLocation
output acsEndpoint                string = 'https://${acsName}.communication.azure.com/'
