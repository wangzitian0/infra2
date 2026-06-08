# Activepieces reads its own rendered secrets at runtime via vault-agent.
# Writes/rotation are performed by the IaC runner and root-token setup, not by
# this service token, so the runtime policy is read-only (least privilege).
path "secret/data/platform/{{env}}/activepieces" {
  capabilities = ["read", "list"]
}
path "secret/metadata/platform/{{env}}/activepieces" {
  capabilities = ["list", "read"]
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
