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

# Per-service token accessors tracked by the Vault app-token lifecycle
# (libs/vault_tokens.py, ACCESSOR_KV_PREFIX = "secret/bootstrap"). The runner reads/writes
# these to track and rotate service tokens during sync. Without this grant the runner token
# is rejected with "permission denied" on
# secret/data/bootstrap/<env>/vault_token_accessors/<project>/<service>, so token refresh
# silently fails and recreated services lose VAULT_ROLE_ID / VAULT_SECRET_ID.
path "secret/data/bootstrap/+/vault_token_accessors/*" {
  capabilities = ["create", "read", "update", "list"]
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

# KV v2 LIST resolves to the secret/metadata/ path, not secret/data/, so the `list`
# capabilities above are no-ops for actual enumeration. Grant metadata read/list for the
# paths the runner lists (service secrets + tracked accessors).
path "secret/metadata/bootstrap/+/vault_token_accessors/*" {
  capabilities = ["read", "list"]
}
path "secret/metadata/platform/+/*" {
  capabilities = ["read", "list"]
}
path "secret/metadata/finance_report/+/*" {
  capabilities = ["read", "list"]
}

path "auth/token/renew-self" {
  capabilities = ["update"]
}
