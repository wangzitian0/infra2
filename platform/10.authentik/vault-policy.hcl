# Authentik needs access to its own secrets
path "secret/data/platform/{{env}}/authentik" {
  capabilities = ["create", "read", "update", "delete", "list"]
}
path "secret/metadata/platform/{{env}}/authentik" {
  capabilities = ["list", "read", "delete"]
}

# Authentik needs to read Postgres and Redis credentials
path "secret/data/platform/{{env}}/postgres" {
  capabilities = ["read", "list"]
}
path "secret/data/platform/{{env}}/redis" {
  capabilities = ["read", "list"]
}

# Required for vault-agent token_file auth to validate the token
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
