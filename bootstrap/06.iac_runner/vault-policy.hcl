# Policy for IaC Runner service
# Allows read access to its own secrets and all platform/finance_report secrets
# for syncing across environments (staging/production)

# Required for vault-agent token validation
path "auth/token/lookup-self" {
  capabilities = ["read"]
}

# Bootstrap secrets - IaC Runner's own configuration
path "secret/data/bootstrap/+/iac_runner" {
  capabilities = ["read", "list"]
}

# Platform secrets for syncing all platform services.
# Sync tasks may repair missing runtime fields before deploying; deletion stays
# reserved for operator/root-token maintenance.
path "secret/data/platform/+/*" {
  capabilities = ["create", "read", "update", "list"]
}

# Finance Report secrets for syncing app services.
# Sync tasks may repair missing runtime fields before deploying; deletion stays
# reserved for operator/root-token maintenance.
path "secret/data/finance_report/+/*" {
  capabilities = ["create", "read", "update", "list"]
}

path "auth/token/renew-self" {
  capabilities = ["update"]
}
