// infra/phase10-vmhost/main.bicep
// Phase 10 — Data-generation + Caddy/TLS VM (CREATED in the billable RG, DELETED on wipe).
//
// One small Ubuntu VM does double duty:
//   1. Runs the dataset + SOP generation keylessly via its SYSTEM-ASSIGNED managed identity
//      (no key, no GitHub secret ever lands on the VM).
//   2. Hosts Caddy, which terminates TLS using the committed Let's Encrypt certificate for the
//      stable host  rmassist.<persistent-ip>.nip.io.
//
// It ASSOCIATES the persistent static public IP (created by infra/persistent, in a different
// RG) so the hostname — and therefore the committed cert — stays valid across full wipes.
// Everything here lives in the billable RG and is removed by the full-purge wipe.

targetScope = 'resourceGroup'

@description('Azure region (must match the persistent static IP region).')
param location string = resourceGroup().location

@description('VM name.')
param vmName string = 'vm-rmx-host'

@description('NIC name.')
param nicName string = 'nic-rmx-host'

@description('NSG name.')
param nsgName string = 'nsg-rmx-host'

@description('VNet name.')
param vnetName string = 'vnet-rmx-host'

@description('VM size.')
param vmSize string = 'Standard_D4as_v5'

@description('Resource ID of the SHARED platform user-assigned managed identity (phase1). Attached to the VM so the app services inherit the Foundry/Search/ACS role assignments phase2 already granted to it.')
param uamiResourceId string

@description('Admin username for SSH.')
param adminUsername string = 'azureuser'

@description('SSH public key (OpenSSH format) authorised for the admin user. Password auth is disabled.')
param sshPublicKey string

@description('Resource ID of the persistent static public IP to associate with this VM NIC.')
param persistPipId string

@description('Base64-encoded cloud-init (customData) that installs Caddy + Python + generation deps.')
param cloudInitBase64 string

@description('Project tag value applied to every resource.')
param projectTag string = 'contoso-retail-rm-assist-rakesh'

var commonTags = {
  project: projectTag
  phase: 'phase10'
}

resource nsg 'Microsoft.Network/networkSecurityGroups@2023-09-01' = {
  name: nsgName
  location: location
  tags: commonTags
  properties: {
    securityRules: [
      {
        name: 'Allow-SSH'
        properties: {
          priority: 1000
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourcePortRange: '*'
          destinationPortRange: '22'
          sourceAddressPrefix: 'Internet'
          destinationAddressPrefix: '*'
        }
      }
      {
        name: 'Allow-HTTP'
        properties: {
          priority: 1010
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourcePortRange: '*'
          destinationPortRange: '80'
          sourceAddressPrefix: 'Internet'
          destinationAddressPrefix: '*'
        }
      }
      {
        name: 'Allow-HTTPS'
        properties: {
          priority: 1020
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourcePortRange: '*'
          destinationPortRange: '443'
          sourceAddressPrefix: 'Internet'
          destinationAddressPrefix: '*'
        }
      }
    ]
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2023-09-01' = {
  name: vnetName
  location: location
  tags: commonTags
  properties: {
    addressSpace: {
      addressPrefixes: [ '10.72.0.0/24' ]
    }
    subnets: [
      {
        name: 'default'
        properties: {
          addressPrefix: '10.72.0.0/24'
          networkSecurityGroup: { id: nsg.id }
        }
      }
    ]
  }
}

resource nic 'Microsoft.Network/networkInterfaces@2023-09-01' = {
  name: nicName
  location: location
  tags: commonTags
  properties: {
    ipConfigurations: [
      {
        name: 'ipconfig1'
        properties: {
          privateIPAllocationMethod: 'Dynamic'
          subnet: { id: vnet.properties.subnets[0].id }
          publicIPAddress: { id: persistPipId }   // borrow the persistent static IP.
        }
      }
    ]
  }
}

resource vm 'Microsoft.Compute/virtualMachines@2023-09-01' = {
  name: vmName
  location: location
  tags: commonTags
  identity: {
    // BOTH identities are attached, deliberately:
    //   SystemAssigned — the VM's own identity, granted Cognitive Services roles by up.sh
    //                    for keyless gpt-5.4 dataset/SOP generation.
    //   UserAssigned   — the SHARED platform UAMI from phase1. Every phase2-ai role
    //                    assignment (Foundry, AI Search, ACS) is keyed to uamiPrincipalId,
    //                    and phase2 runs BEFORE this VM exists — so re-keying those grants
    //                    to a VM principal would be circular. Attaching the existing UAMI
    //                    instead keeps all of phase2 unchanged and lets the app services
    //                    reuse exactly the permissions they had as Container Apps.
    //
    // With two identities present, DefaultAzureCredential cannot guess which to use: the
    // app units set AZURE_CLIENT_ID to the UAMI's client id (the same thing phase9 did for
    // the Container App), which pins ManagedIdentityCredential to the UAMI.
    type: 'SystemAssigned, UserAssigned'
    userAssignedIdentities: {
      '${uamiResourceId}': {}
    }
  }
  properties: {
    hardwareProfile: { vmSize: vmSize }
    osProfile: {
      computerName: vmName
      adminUsername: adminUsername
      customData: cloudInitBase64
      linuxConfiguration: {
        disablePasswordAuthentication: true
        ssh: {
          publicKeys: [
            {
              path: '/home/${adminUsername}/.ssh/authorized_keys'
              keyData: sshPublicKey
            }
          ]
        }
      }
    }
    storageProfile: {
      imageReference: {
        publisher: 'Canonical'
        offer: '0001-com-ubuntu-server-jammy'
        sku: '22_04-lts-gen2'
        version: 'latest'
      }
      osDisk: {
        createOption: 'FromImage'
        managedDisk: { storageAccountType: 'StandardSSD_LRS' }
        diskSizeGB: 32
      }
    }
    networkProfile: {
      networkInterfaces: [ { id: nic.id } ]
    }
  }
}

@description('The VM system-assigned managed identity principal ID (granted keyless gpt-5.4 access).')
output vmPrincipalId string = vm.identity.principalId
@description('VM resource ID.')
output vmId string = vm.id
