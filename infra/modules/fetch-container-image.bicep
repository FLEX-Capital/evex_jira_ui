// Returns the containers of an existing container app, so `azd provision` keeps the image
// that `azd deploy` pushed instead of resetting it to the bootstrap placeholder.
param exists bool
param name string

resource existingApp 'Microsoft.App/containerApps@2024-03-01' existing = if (exists) {
  name: name
}

// `!` is safe: the reference is only evaluated when `exists` is true.
output containers array = exists ? existingApp!.properties.template.containers : []
