# Generated from platform/redis by infra2_sdk.secrets. Do not edit.

path "secret/data/platform/{{env}}/redis" {
  capabilities = ["read"]
}

path "secret/metadata/platform/{{env}}/redis" {
  capabilities = ["read", "list"]
}

path "auth/token/lookup-self" {
  capabilities = ["read"]
}
