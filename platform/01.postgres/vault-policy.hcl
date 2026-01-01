path "secret/data/platform/{{env}}/postgres" {
  capabilities = ["read"]
}
path "secret/metadata/platform/{{env}}/postgres" {
  capabilities = ["read", "list"]
}
# Required for vault-agent token_file auth to validate the token
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
