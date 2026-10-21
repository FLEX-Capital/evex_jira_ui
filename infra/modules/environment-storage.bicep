// Links the Jira UI file share to its Container Apps environment, deployed into that
// environment's resource group. A shared environment (cae-evex, Terraform-managed in
// evex_customer_support) is only referenced here, never modified.
param environmentName string
param name string
param accountName string

@secure()
param accountKey string

param shareName string

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: environmentName
}

resource storage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: environment
  name: name
  properties: {
    azureFile: {
      accountName: accountName
      accountKey: accountKey
      shareName: shareName
      accessMode: 'ReadWrite'
    }
  }
}

output environmentId string = environment.id
output name string = storage.name
