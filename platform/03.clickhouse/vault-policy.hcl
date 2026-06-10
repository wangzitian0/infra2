# Required for vault-agent token validation
path "auth/token/lookup-self" {
  capabilities = ["read"]
}

path "secret/data/platform/{{env}}/clickhouse" {
  capabilities = ["read", "list"]
}

path "auth/token/renew-self" {
  capabilities = ["update"]
}
