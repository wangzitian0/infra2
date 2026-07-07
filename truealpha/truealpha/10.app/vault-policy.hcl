# Policy for truealpha app service
# Scoped by vault.setup-approle to the target deployment environment.
path "secret/data/truealpha/{{env}}/app" {
  capabilities = ["read"]
}

path "secret/metadata/truealpha/{{env}}/app" {
  capabilities = ["read", "list"]
}

# Required for dynamic DATABASE_URL construction
path "secret/data/truealpha/{{env}}/postgres" {
  capabilities = ["read"]
}

path "secret/metadata/truealpha/{{env}}/postgres" {
  capabilities = ["read", "list"]
}

# Required for the vault-agent healthcheck token lookup (AppRole auth)
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
