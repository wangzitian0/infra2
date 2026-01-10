# Policy for finance_report app service
path "secret/data/finance_report/+/app" {
  capabilities = ["read"]
}

path "secret/metadata/finance_report/+/app" {
  capabilities = ["read", "list"]
}
