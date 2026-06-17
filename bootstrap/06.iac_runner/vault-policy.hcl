# Policy for IaC Runner service (AppRole; see docs/ssot/bootstrap.iac_runner.md §6.4)
# Allows read access to its own secrets and all platform/finance_report secrets
# for syncing across environments (staging/production).
#
# v2 cleanup (#369): the `vault_token_accessors` CRUD grant and `auth/token/renew-self`
# were removed once iac-runner moved to AppRole. The accessor ledger only ever tracked
# legacy static tokens (no service uses them anymore), and AppRole vault-agents
# re-authenticate via `auth/approle/login` rather than `renew-self`.

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

# KV v2 LIST resolves to the secret/metadata/ path, not secret/data/, so the `list`
# capabilities above are no-ops for actual enumeration. Grant metadata read/list for the
# service-secret paths the runner lists.
path "secret/metadata/platform/+/*" {
  capabilities = ["read", "list"]
}
path "secret/metadata/finance_report/+/*" {
  capabilities = ["read", "list"]
}
