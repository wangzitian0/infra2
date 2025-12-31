path "secret/data/platform/{{env}}/postgres" {
  capabilities = ["create", "read", "update", "delete", "list"]
}
path "secret/metadata/platform/{{env}}/postgres" {
  capabilities = ["list", "read", "delete"]
}
