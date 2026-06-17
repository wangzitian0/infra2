path "secret/data/platform/{{env}}/postgres" {
  capabilities = ["read"]
}
path "secret/metadata/platform/{{env}}/postgres" {
  capabilities = ["read", "list"]
}
# Required for the vault-agent healthcheck token lookup (AppRole auth)
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
