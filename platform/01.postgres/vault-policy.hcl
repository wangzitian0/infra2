path "secret/data/platform/{{env}}/postgres" {
  capabilities = ["create", "read", "update", "delete", "list"]
}
path "secret/metadata/platform/{{env}}/postgres" {
  capabilities = ["list", "read", "delete"]
}
# Required for vault-agent token_file auth to validate the token
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
