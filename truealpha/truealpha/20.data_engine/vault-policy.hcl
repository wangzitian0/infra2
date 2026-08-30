# Scoped by vault.setup-approle to one deployment environment.
path "secret/data/truealpha/{{env}}/data_engine" {
  capabilities = ["read"]
}

path "secret/metadata/truealpha/{{env}}/data_engine" {
  capabilities = ["read", "list"]
}

path "secret/data/truealpha/{{env}}/postgres" {
  capabilities = ["read"]
}

path "secret/metadata/truealpha/{{env}}/postgres" {
  capabilities = ["read", "list"]
}

path "secret/data/truealpha/{{env}}/market-data" {
  capabilities = ["read"]
}

path "secret/metadata/truealpha/{{env}}/market-data" {
  capabilities = ["read", "list"]
}

path "auth/token/lookup-self" {
  capabilities = ["read"]
}
