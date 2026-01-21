# Policy for IaC Runner service
# Allows read access to its own secrets

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
