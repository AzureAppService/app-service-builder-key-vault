@description('A globally unique name for a new, dedicated development/test vault.')
@minLength(3)
@maxLength(24)
param vaultName string

param location string = resourceGroup().location

resource vault 'Microsoft.KeyVault/vaults@2024-11-01' = {
  name: vaultName
  location: location
  properties: {
    tenantId: tenant().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    enablePurgeProtection: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'None'
      defaultAction: 'Allow'
    }
  }
}

output vaultUrl string = vault.properties.vaultUri
output vaultId string = vault.id
