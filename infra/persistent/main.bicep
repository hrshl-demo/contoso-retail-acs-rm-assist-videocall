// infra/persistent/main.bicep
// Persistent layer — the ONE resource that must survive a full wipe.
//
// Creates a STATIC, Standard-SKU public IP in the persistent resource group. Its address
// anchors the stable Let's Encrypt hostname  rmassist.<ip>.nip.io, which is what makes the
// committed certificate reusable across full teardowns of the billable RG. The billable
// data-gen/Caddy VM (infra/phase10-vmhost) associates THIS IP each build; wipe deletes the
// VM but never this RG, so the IP — and therefore the cert's domain — never changes.
//
// Deliberately minimal: a public IP costs a few USD/month reserved, which is the price of a
// stable demo hostname + a reusable TLS certificate.

targetScope = 'resourceGroup'

@description('Azure region for the persistent static IP (must match the billable VM region).')
param location string = resourceGroup().location

@description('Name of the static public IP.')
param pipName string = 'pip-rmx-persist'

@description('Project tag value applied to the persistent resource(s).')
param projectTag string = 'contoso-retail-rm-assist-rakesh-persistent'

resource pip 'Microsoft.Network/publicIPAddresses@2023-09-01' = {
  name: pipName
  location: location
  sku: {
    name: 'Standard'      // Standard SKU => static allocation, zone-resilient, associable to a VM NIC.
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
    idleTimeoutInMinutes: 4
  }
  tags: {
    project: projectTag
    purpose: 'rmassist-static-ip-nip-io-anchor'
  }
}

@description('The allocated static IP address (anchors rmassist.<ip>.nip.io).')
output persistIp string = pip.properties.ipAddress
@description('The resource ID of the static public IP (associated by the billable VM each build).')
output persistPipId string = pip.id
