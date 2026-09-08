# Generated from platform/postgres by infra2_sdk.secrets. Do not edit.

path "secret/data/platform/{{env}}/postgres" {
  capabilities = ["read"]
}

path "secret/metadata/platform/{{env}}/postgres" {
  capabilities = ["read", "list"]
}

path "auth/token/renew-self" {
  capabilities = ["update"]
}

path "auth/token/lookup-self" {
  capabilities = ["read"]
}
