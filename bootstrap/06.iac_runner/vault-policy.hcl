# Policy for IaC Runner service (AppRole; see docs/ssot/bootstrap.iac_runner.md §6.4)
# Allows read access to its own secrets and all platform/finance_report secrets
# for syncing across environments (staging/production).
#
# v2 cleanup (#369) retired the `vault_token_accessors` CRUD grant with the static-token
# machinery. It also dropped `auth/token/renew-self` on the assumption that AppRole
# agents "re-authenticate rather than renew" — they do not: Vault Agent renews first and
# re-authenticates only once renewal has failed, so the grant is back (2026-09-08, #643).

# Vault Agent renews its own token through renew-self (the LifetimeWatcher renews at once,
# then ahead of every TTL). The AppRoles carry token_no_default_policy=true, so the grant
# the `default` policy would give has to be here; without it every renewal is a 403 that
# Vault Agent 1.15 retries without backoff (2026-09-08: six sidecars at ~35 requests/s).
path "auth/token/renew-self" {
  capabilities = ["update"]
}

# Required for vault-agent token validation
path "auth/token/lookup-self" {
  capabilities = ["read"]
}

# Bootstrap secrets - IaC Runner's own configuration
path "secret/data/bootstrap/+/iac_runner" {
  capabilities = ["read", "list"]
}

# `patch`: infra2-sdk's secret writer (>= 1.5.0) sends an HTTP PATCH
# (application/merge-patch+json) whenever the secret already exists, so only the changed
# keys travel and a key this deploy does not know about is never clobbered. KV v2 gates
# that verb on its own capability: without it every re-deploy of an existing service
# fails `vault_permission_denied` (truealpha v0.0.49 staging, 2026-09-08).
# Platform secrets for syncing all platform services.
# Sync tasks may repair missing runtime fields before deploying; deletion stays
# reserved for operator/root-token maintenance.
path "secret/data/platform/+/*" {
  capabilities = ["create", "read", "update", "patch", "list"]
}

# Finance Report secrets for syncing app services.
# Sync tasks may repair missing runtime fields before deploying; deletion stays
# reserved for operator/root-token maintenance.
path "secret/data/finance_report/+/*" {
  capabilities = ["create", "read", "update", "patch", "list"]
}

# TrueAlpha secrets for syncing app services (same rationale as finance_report).
path "secret/data/truealpha/+/*" {
  capabilities = ["create", "read", "update", "patch", "list"]
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
path "secret/metadata/truealpha/+/*" {
  capabilities = ["read", "list"]
}
