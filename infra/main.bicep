// EVEX Jira UI on Azure Container Apps. Deployed with `azd up`; see README "Deploying to Azure".
targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('azd environment name; seeds the unique resource names.')
param environmentName string

@minLength(1)
@description('Azure region for all resources.')
param location string

@description('Resource group that holds every resource of this app.')
param resourceGroupName string = 'evex_jira_ui'

// A resource group's own region is fixed at creation and is metadata only: evex_jira_ui in
// "EVEX n8n at FLEX" was created in germanywestcentral and holds West Europe resources.
@description('Region of the resource group itself; empty uses location.')
param resourceGroupLocation string = ''

@description('Readable base of every resource name, e.g. cae-evex-jira-ui.')
param baseName string = 'evex-jira-ui'

@description('Existing Container Apps environment to share; empty creates a dedicated one.')
param containerAppsEnvironmentName string = ''

@description('Resource group of the shared Container Apps environment.')
param containerAppsEnvironmentResourceGroup string = ''

@description('Jira base URL, e.g. https://example.atlassian.net.')
param jiraUrl string

@secure()
@description('Jira account user name.')
param jiraUsername string

@secure()
@description('Jira API token.')
param jiraPassword string

// The dashboard is reachable on a public URL and the password gate is its only protection
// (ui_auth.py disables the gate when UI_PASSWORD is empty), so an empty or short value
// must fail the deployment instead of publishing an open dashboard.
@secure()
@minLength(12)
@description('Shared dashboard password (UI_PASSWORD). At least 12 characters.')
param uiPassword string

@description('Set by azd once the container app exists, so provisioning keeps the deployed image.')
param jiraUiExists bool = false

var tags = { 'azd-env-name': environmentName }

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: empty(resourceGroupLocation) ? location : resourceGroupLocation
  tags: tags
}

module resources 'modules/resources.bicep' = {
  name: 'jira-ui-resources'
  scope: rg
  params: {
    location: location
    baseName: baseName
    tags: tags
    containerAppsEnvironmentName: containerAppsEnvironmentName
    containerAppsEnvironmentResourceGroup: containerAppsEnvironmentResourceGroup
    jiraUrl: jiraUrl
    jiraUsername: jiraUsername
    jiraPassword: jiraPassword
    uiPassword: uiPassword
    jiraUiExists: jiraUiExists
  }
}

output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = resources.outputs.registryLoginServer
output AZURE_CONTAINER_REGISTRY_NAME string = resources.outputs.registryName
output SERVICE_JIRA_UI_NAME string = resources.outputs.appName
output SERVICE_JIRA_UI_URI string = resources.outputs.appUri
