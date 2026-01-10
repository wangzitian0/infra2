# Policy for finance_report redis service
path "secret/data/finance_report/{{env}}/redis" {
  capabilities = ["read"]
}

path "secret/metadata/finance_report/{{env}}/redis" {
  capabilities = ["read", "list"]
}

# Required for vault-agent token_file auth to validate the token
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
