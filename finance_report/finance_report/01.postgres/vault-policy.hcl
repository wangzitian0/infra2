# Generated from finance_report/postgres by infra2_sdk.secrets. Do not edit.

path "secret/data/finance_report/{{env}}/postgres" {
  capabilities = ["read"]
}

path "secret/metadata/finance_report/{{env}}/postgres" {
  capabilities = ["read", "list"]
}

path "auth/token/lookup-self" {
  capabilities = ["read"]
}
