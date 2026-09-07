# Generated from platform/prefect by infra2_sdk.secrets. Do not edit.

path "secret/data/platform/{{env}}/prefect" {
  capabilities = ["read"]
}

path "secret/metadata/platform/{{env}}/prefect" {
  capabilities = ["read", "list"]
}

path "secret/data/platform/{{env}}/postgres" {
  capabilities = ["read"]
}

path "secret/metadata/platform/{{env}}/postgres" {
  capabilities = ["read", "list"]
}

path "secret/data/platform/{{env}}/redis" {
  capabilities = ["read"]
}

path "secret/metadata/platform/{{env}}/redis" {
  capabilities = ["read", "list"]
}

path "auth/token/lookup-self" {
  capabilities = ["read"]
}
