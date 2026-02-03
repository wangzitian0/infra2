# Policy for finance_report app service
# Uses wildcard (+) to allow access to any environment
# This allows the same policy/token to work for production, staging, test, etc.
path "secret/data/finance_report/+/app" {
  capabilities = ["read"]
}

path "secret/metadata/finance_report/+/app" {
  capabilities = ["read", "list"]
}

# Required for dynamic DATABASE_URL and REDIS_URL construction
path "secret/data/finance_report/+/postgres" {
  capabilities = ["read"]
}

path "secret/metadata/finance_report/+/postgres" {
  capabilities = ["read", "list"]
}

path "secret/data/finance_report/+/redis" {
  capabilities = ["read"]
}

path "secret/metadata/finance_report/+/redis" {
  capabilities = ["read", "list"]
}

# Required for vault-agent token_file auth to validate the token
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
