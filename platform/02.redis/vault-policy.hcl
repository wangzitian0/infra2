path "secret/data/platform/{{env}}/redis" {
  capabilities = ["read"]
}
path "secret/metadata/platform/{{env}}/redis" {
  capabilities = ["read", "list"]
}
# Required for vault-agent token_file auth to validate the token
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
