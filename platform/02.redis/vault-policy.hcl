path "secret/data/platform/{{env}}/redis" {
  capabilities = ["create", "read", "update", "delete", "list"]
}
path "secret/metadata/platform/{{env}}/redis" {
  capabilities = ["list", "read", "delete"]
}
# Required for vault-agent token_file auth to validate the token
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
