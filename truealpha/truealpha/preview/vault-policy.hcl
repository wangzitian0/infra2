# Generated from truealpha/app by infra2_sdk.secrets. Do not edit.

path "secret/data/truealpha/staging/app" {
  capabilities = ["read"]
}

path "secret/metadata/truealpha/staging/app" {
  capabilities = ["read", "list"]
}

path "auth/token/renew-self" {
  capabilities = ["update"]
}

path "auth/token/lookup-self" {
  capabilities = ["read"]
}
