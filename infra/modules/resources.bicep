// All resources of the Jira UI, deployed into the evex_jira_ui resource group. The app gets
// its own Container Apps environment, unless containerAppsEnvironmentName names an existing
// one to share (the interim deployment in "Azure subscription 1" uses cae-evex, because a
// new environment there failed with AKSCapacityHeavyUsage).
//
// The deployment must work for a Contributor, who cannot create role assignments. So the app
// pulls images with the registry admin credential, and reads Key Vault through an access
// policy instead of RBAC. Switching to role-based access later needs no app code change.
param location string
@description('Readable base of every resource name, e.g. cae-evex-jira-ui.')
@minLength(3)
@maxLength(21)
param baseName string
param tags object
@description('Existing environment to share; empty creates a dedicated one.')
param containerAppsEnvironmentName string

@description('Resource group of the shared environment; ignored for a dedicated one.')
param containerAppsEnvironmentResourceGroup string

param jiraUrl string

@secure()
param jiraUsername string

@secure()
param jiraPassword string

@secure()
param uiPassword string

param jiraUiExists bool

// Registry and storage account names allow no hyphens.
var compactName = replace(baseName, '-', '')
var serviceName = 'jira-ui'
var appName = 'ca-${baseName}'
var appPort = 8501
var healthPath = '/_stcore/health'
// azd replaces this on the first `azd deploy`; fetchImage keeps the real image afterwards.
var placeholderImage = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
var dedicatedEnvironment = empty(containerAppsEnvironmentName)
var environmentRg = dedicatedEnvironment ? resourceGroup().name : containerAppsEnvironmentResourceGroup

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = if (dedicatedEnvironment) {
  name: 'log-${baseName}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = if (dedicatedEnvironment) {
  name: 'cae-${baseName}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs!.properties.customerId
        sharedKey: logs!.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  // checkov:skip=CKV_AZURE_137: admin credential is required; Contributor cannot grant AcrPull
  // checkov:skip=CKV_AZURE_139: Basic SKU has no private endpoints; no VNet in this deployment
  // checkov:skip=CKV_AZURE_163: image scanning needs Defender for Containers (not enabled)
  // checkov:skip=CKV_AZURE_166: quarantine/trust policies need the Premium SKU
  // replace() hides the length from Bicep; the default gives 12 characters.
  #disable-next-line BCP334
  name: 'cr${compactName}'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: {
    // Contributor cannot grant AcrPull to a managed identity (see header).
    adminUserEnabled: true
  }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${baseName}'
  location: location
  tags: tags
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  // checkov:skip=CKV_AZURE_109: the Consumption environment has no VNet or fixed egress IP,
  // checkov:skip=CKV_AZURE_189: so the vault stays public; access is Entra auth + one policy
  name: 'kv-${baseName}'
  location: location
  tags: tags
  properties: {
    tenantId: tenant().tenantId
    sku: { family: 'A', name: 'standard' }
    // Access policies instead of RBAC: Contributor cannot create role assignments.
    enableRbacAuthorization: false
    accessPolicies: [
      {
        tenantId: tenant().tenantId
        objectId: identity.properties.principalId
        permissions: { secrets: ['get'] }
      }
    ]
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    // A deleted vault stays recoverable (and its name reserved) for 7 days.
    enablePurgeProtection: true
  }
}

resource jiraUsernameSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  // checkov:skip=CKV_AZURE_41: an expiry would break the app; rotate with the Jira token
  name: 'jira-username'
  properties: { contentType: 'text/plain', value: jiraUsername }
}

resource jiraPasswordSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  // checkov:skip=CKV_AZURE_41: an expiry would break the app; rotate with the Jira token
  name: 'jira-password'
  properties: { contentType: 'text/plain', value: jiraPassword }
}

resource uiPasswordSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  // checkov:skip=CKV_AZURE_41: an expiry would break the app; rotate with the Jira token
  name: 'ui-password'
  properties: { contentType: 'text/plain', value: uiPassword }
}

// data/ (ticket cache, import checkpoints, status) lives on this share so it survives
// restarts and new revisions.
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  // checkov:skip=CKV_AZURE_35: the SMB mount comes from a Consumption environment without a
  // VNet or fixed egress IP, so a deny-by-default firewall would block it
  // checkov:skip=CKV_AZURE_43: false positive; the interpolated name is lowercase alphanumeric
  // checkov:skip=CKV_AZURE_206: LRS is enough; the share is a cache re-importable from Jira
  // replace() hides the length from Bicep; the default gives 12 characters.
  #disable-next-line BCP334
  name: 'st${compactName}'
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    // The Container Apps SMB mount authenticates with the account key.
    allowSharedKeyAccess: true
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    shareDeleteRetentionPolicy: { enabled: true, days: 7 }
  }
}

resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileService
  name: 'jira-data'
  properties: {
    enabledProtocols: 'SMB'
    shareQuota: 50
  }
}

// A shared environment keeps logging to its own Log Analytics workspace.
module environmentStorage 'environment-storage.bicep' = {
  name: 'jira-ui-environment-storage'
  scope: resourceGroup(environmentRg)
  params: {
    environmentName: dedicatedEnvironment ? environment.name : containerAppsEnvironmentName
    // Prefixed: in a shared environment the name must say whose share this is.
    name: 'jira-ui-data'
    accountName: storage.name
    accountKey: storage.listKeys().keys[0].value
    shareName: share.name
  }
}

module fetchImage 'fetch-container-image.bicep' = {
  name: 'fetch-jira-ui-image'
  params: {
    exists: jiraUiExists
    name: appName
  }
}

var deployedContainers = fetchImage.outputs.containers

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  tags: union(tags, { 'azd-service-name': serviceName })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    environmentId: environmentStorage.outputs.environmentId
    // The shared cae-evex is Consumption-only: it has no workload profiles to name.
    workloadProfileName: dedicatedEnvironment ? 'Consumption' : null
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: appPort
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: registry.properties.loginServer
          username: registry.listCredentials().username
          passwordSecretRef: 'registry-password'
        }
      ]
      secrets: [
        { name: 'registry-password', value: registry.listCredentials().passwords[0].value }
        { name: 'jira-username', keyVaultUrl: jiraUsernameSecret.properties.secretUri, identity: identity.id }
        { name: 'jira-password', keyVaultUrl: jiraPasswordSecret.properties.secretUri, identity: identity.id }
        { name: 'ui-password', keyVaultUrl: uiPasswordSecret.properties.secretUri, identity: identity.id }
      ]
    }
    template: {
      containers: [
        {
          name: serviceName
          image: empty(deployedContainers) ? placeholderImage : deployedContainers[0].image
          resources: { cpu: json('1.0'), memory: '2Gi' }
          env: [
            { name: 'JIRA_URL', value: jiraUrl }
            { name: 'JIRA_USERNAME', secretRef: 'jira-username' }
            { name: 'JIRA_PASSWORD', secretRef: 'jira-password' }
            { name: 'UI_PASSWORD', secretRef: 'ui-password' }
          ]
          probes: [
            {
              type: 'Startup'
              httpGet: { path: healthPath, port: appPort }
              initialDelaySeconds: 5
              periodSeconds: 10
              timeoutSeconds: 5
              failureThreshold: 30
            }
            {
              type: 'Liveness'
              httpGet: { path: healthPath, port: appPort }
              periodSeconds: 30
              timeoutSeconds: 5
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: { path: healthPath, port: appPort }
              periodSeconds: 10
              timeoutSeconds: 5
              failureThreshold: 3
            }
          ]
          volumeMounts: [{ volumeName: 'jira-data', mountPath: '/app/data' }]
        }
      ]
      // Exactly one replica: the import worker is a thread in the Streamlit process and the
      // cache locks assume a single process. While only the placeholder exists (first
      // provision) no replica runs, because it would fail the probes on port 8501; the
      // next `azd provision` after the first deploy pins the always-on replica.
      scale: {
        minReplicas: jiraUiExists ? 1 : 0
        maxReplicas: 1
      }
      volumes: [
        {
          name: 'jira-data'
          storageType: 'AzureFile'
          storageName: environmentStorage.outputs.name
          // Owned by the image's appuser (uid/gid 10001 in the Dockerfile).
          mountOptions: 'uid=10001,gid=10001,dir_mode=0750,file_mode=0640'
        }
      ]
    }
  }
}

output registryLoginServer string = registry.properties.loginServer
output registryName string = registry.name
output appName string = app.name
output appUri string = 'https://${app.properties.configuration.ingress.fqdn}'
