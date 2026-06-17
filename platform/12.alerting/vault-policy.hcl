# Policy for platform alerting bridge.
# Scoped by vault.setup-approle to the target deployment environment.
path "secret/data/platform/{{env}}/alerting" {
  capabilities = ["read"]
}

path "secret/metadata/platform/{{env}}/alerting" {
  capabilities = ["read", "list"]
}

# Required for the vault-agent healthcheck token lookup (AppRole auth).
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
