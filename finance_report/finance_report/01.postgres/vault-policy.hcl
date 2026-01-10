# Policy for finance_report postgres service
path "secret/data/finance_report/+/postgres" {
  capabilities = ["read"]
}

path "secret/metadata/finance_report/+/postgres" {
  capabilities = ["read", "list"]
}
