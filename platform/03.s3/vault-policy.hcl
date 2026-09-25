# Generated from platform/minio by infra2_sdk.secrets. Do not edit.

path "secret/data/platform/{{env}}/minio" {
  capabilities = ["read"]
}

path "secret/metadata/platform/{{env}}/minio" {
  capabilities = ["read", "list"]
}

path "auth/token/renew-self" {
  capabilities = ["update"]
}

path "auth/token/lookup-self" {
  capabilities = ["read"]
}
