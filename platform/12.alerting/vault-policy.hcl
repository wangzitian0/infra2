# Generated from platform/alerting by infra2_sdk.secrets. Do not edit.

path "secret/data/platform/{{env}}/alerting" {
  capabilities = ["read"]
}

path "secret/metadata/platform/{{env}}/alerting" {
  capabilities = ["read", "list"]
}

path "auth/token/renew-self" {
  capabilities = ["update"]
}

path "auth/token/lookup-self" {
  capabilities = ["read"]
}
