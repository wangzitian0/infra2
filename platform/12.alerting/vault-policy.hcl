# Policy for platform alerting bridge.
# Scoped by vault.setup-approle to the target deployment environment.
path "secret/data/platform/{{env}}/alerting" {
  capabilities = ["read"]
}

path "secret/metadata/platform/{{env}}/alerting" {
  capabilities = ["read", "list"]
}

# Required for vault-agent token_file auth to validate the token.
path "auth/token/lookup-self" {
  capabilities = ["read"]
}

path "auth/token/renew-self" {
  capabilities = ["update"]
}
