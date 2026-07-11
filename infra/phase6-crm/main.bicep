// infra/phase6-crm/main.bicep
// Phase 6 — CRM dashboard (RM cockpit) as a Container App.
// Static nginx app; TOOLAPI_URL + bearer injected at startup. External HTTPS, port 8080.

targetScope = 'resourceGroup'

param location string = resourceGroup().location
param projectTag string = 'contoso-retail-rm-assist-rakesh'
param acaEnvName string
param acrName string
param appName string
param imageRef string
param uamiResourceId string
@secure()
@description('Tool API bearer token (from Phase 4 outputs).')
param toolapiBearerToken string
@description('Public URL of the Tool API (from Phase 4 outputs).')
param toolapiUrl string
@description('Public URL of the Video Assist app (Step 7 video call, from Phase 9 outputs). Empty until phase9 has run.')
param videoassistUrl string = ''

resource cae 'Microsoft.App/managedEnvironments@2024-03-01' existing = { name: acaEnvName }
resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' existing = { name: acrName }

var commonTags = { project: projectTag, phase: 'phase6' }

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  tags: commonTags
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${uamiResourceId}': {} } }
  properties: {
    managedEnvironmentId: cae.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8080
        transport: 'auto'
        allowInsecure: false
        traffic: [ { latestRevision: true, weight: 100 } ]
      }
      registries: [ { server: acr.properties.loginServer, identity: uamiResourceId } ]
      secrets: [
        { name: 'toolapi-bearer-token', value: toolapiBearerToken }
      ]
    }
    template: {
      containers: [
        {
          name: 'dashboard'
          image: imageRef
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: [
            { name: 'TOOLAPI_URL', value: toolapiUrl }
            { name: 'TOOLAPI_BEARER', secretRef: 'toolapi-bearer-token' }
            { name: 'VIDEOASSIST_URL', value: videoassistUrl }
          ]
          probes: [
            { type: 'Liveness',  httpGet: { path: '/healthz', port: 8080 }, initialDelaySeconds: 5, periodSeconds: 30 }
            { type: 'Readiness', httpGet: { path: '/healthz', port: 8080 }, initialDelaySeconds: 3, periodSeconds: 10 }
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 2 }
    }
  }
}

output appFqdn string = app.properties.configuration.ingress.fqdn
output appUrl  string = 'https://${app.properties.configuration.ingress.fqdn}'
