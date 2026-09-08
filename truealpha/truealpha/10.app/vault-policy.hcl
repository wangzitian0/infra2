# Generated from truealpha/app by infra2_sdk.secrets. Do not edit.

path "secret/data/truealpha/{{env}}/app" {
  capabilities = ["read"]
}

path "secret/metadata/truealpha/{{env}}/app" {
  capabilities = ["read", "list"]
}

path "secret/data/truealpha/{{env}}/postgres" {
  capabilities = ["read"]
}

path "secret/metadata/truealpha/{{env}}/postgres" {
  capabilities = ["read", "list"]
}

path "auth/token/lookup-self" {
  capabilities = ["read"]
}
