path "secret/data/platform/{{env}}/redis" {
  capabilities = ["read"]
}
path "secret/metadata/platform/{{env}}/redis" {
  capabilities = ["read", "list"]
}
# Required for the vault-agent healthcheck token lookup (AppRole auth)
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
