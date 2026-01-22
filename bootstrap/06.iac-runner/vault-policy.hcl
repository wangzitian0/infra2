# Policy for IaC Runner service
# Allows read access to its own secrets

# Required for vault-agent token validation
path "auth/token/lookup-self" {
  capabilities = ["read"]
}

path "secret/data/bootstrap/{{env}}/iac_runner" {
  capabilities = ["read", "list"]
}

# Also need to read platform and finance_report secrets for syncing
path "secret/data/platform/{{env}}/*" {
  capabilities = ["read", "list"]
}

path "secret/data/finance_report/{{env}}/*" {
  capabilities = ["read", "list"]
}
