# Policy for finance_report app service
# Scoped by vault.setup-approle to the target deployment environment.
path "secret/data/finance_report/{{env}}/app" {
  capabilities = ["read"]
}

path "secret/metadata/finance_report/{{env}}/app" {
  capabilities = ["read", "list"]
}

# Required for dynamic DATABASE_URL and REDIS_URL construction
path "secret/data/finance_report/{{env}}/postgres" {
  capabilities = ["read"]
}

path "secret/metadata/finance_report/{{env}}/postgres" {
  capabilities = ["read", "list"]
}

path "secret/data/finance_report/{{env}}/redis" {
  capabilities = ["read"]
}

path "secret/metadata/finance_report/{{env}}/redis" {
  capabilities = ["read", "list"]
}

# Vault Agent renews its own token through renew-self (the LifetimeWatcher renews at once,
# then ahead of every TTL). The AppRoles carry token_no_default_policy=true, so the grant
# the `default` policy would give has to be here; without it every renewal is a 403 that
# Vault Agent 1.15 retries without backoff (2026-09-08: six sidecars at ~35 requests/s).
path "auth/token/renew-self" {
  capabilities = ["update"]
}

# Required for the vault-agent healthcheck token lookup (AppRole auth)
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
