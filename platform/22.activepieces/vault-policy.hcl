# Activepieces needs access to its own secrets
path "secret/data/platform/{{env}}/activepieces" {
  capabilities = ["create", "read", "update", "delete", "list"]
}
path "secret/metadata/platform/{{env}}/activepieces" {
  capabilities = ["list", "read", "delete"]
}

# Activepieces needs to read Postgres and Redis credentials
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
