targetScope = 'resourceGroup'

@description('Unique name for a new source-less Builder App; do not rerun over a configured app.')
param appName string
param vaultName string
@description('Owner-approved future UTC deadline; does not delete resources.')
param expiresAt string
@allowed(['westus2'])
param location string = 'westus2'

resource vault 'Microsoft.KeyVault/vaults@2024-11-01' existing = {
  name: vaultName
}
resource app 'Microsoft.Web/builderApps@2026-09-01-preview' = {
  name: appName
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    versionConfiguration: {
      variables: [
        { name: 'SAMPLE_EXPIRES_AT', value: expiresAt }
      ]
    }
    scaling: {
      components: [
        { name: 'web', minReplicas: 0, maxReplicas: 1, httpEnabled: true }
      ]
    }
  }
}
var secretsUser = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
resource reader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, app.id, secretsUser)
  scope: vault
  properties: {
    principalId: app.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: secretsUser
  }
}

output appResourceId string = app.id
output principalId string = app.identity.principalId
