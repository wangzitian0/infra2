# Policy for finance_report redis service
path "secret/data/finance_report/+/redis" {
  capabilities = ["read"]
}

path "secret/metadata/finance_report/+/redis" {
  capabilities = ["read", "list"]
}
