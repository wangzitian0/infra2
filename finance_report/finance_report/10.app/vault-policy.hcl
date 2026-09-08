# Generated from finance_report/app by infra2_sdk.secrets. Do not edit.

path "secret/data/finance_report/{{env}}/app" {
  capabilities = ["read"]
}

path "secret/metadata/finance_report/{{env}}/app" {
  capabilities = ["read", "list"]
}

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

path "auth/token/renew-self" {
  capabilities = ["update"]
}

path "auth/token/lookup-self" {
  capabilities = ["read"]
}
