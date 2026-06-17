# Authentik reads its own rendered secrets at runtime via vault-agent.
# Writes/rotation are performed by the IaC runner and root-token setup, not by
# this service token, so the runtime policy is read-only (least privilege).
path "secret/data/platform/{{env}}/authentik" {
  capabilities = ["read", "list"]
}
path "secret/metadata/platform/{{env}}/authentik" {
  capabilities = ["list", "read"]
}

# Authentik needs to read Postgres and Redis credentials
path "secret/data/platform/{{env}}/postgres" {
  capabilities = ["read", "list"]
}
path "secret/data/platform/{{env}}/redis" {
  capabilities = ["read", "list"]
}

# Required for the vault-agent healthcheck token lookup (AppRole auth)
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
