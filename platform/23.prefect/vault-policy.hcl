path "secret/data/platform/{{env}}/prefect" {
  capabilities = ["read"]
}
path "secret/metadata/platform/{{env}}/prefect" {
  capabilities = ["read", "list"]
}
# Access to postgres password for database connection
path "secret/data/platform/{{env}}/postgres" {
  capabilities = ["read"]
}
path "secret/metadata/platform/{{env}}/postgres" {
  capabilities = ["read", "list"]
}
# Access to redis password for messaging
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
