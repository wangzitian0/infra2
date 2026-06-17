# Policy for finance_report redis service
# Scoped by vault.setup-approle to the target deployment environment.
path "secret/data/finance_report/{{env}}/redis" {
  capabilities = ["read"]
}

path "secret/metadata/finance_report/{{env}}/redis" {
  capabilities = ["read", "list"]
}

# Required for the vault-agent healthcheck token lookup (AppRole auth)
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
