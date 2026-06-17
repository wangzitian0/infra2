# MinIO app token policy - READ-ONLY for runtime secrets access
# Write/delete permissions are reserved for admin operations via VAULT_ROOT_TOKEN
path "secret/data/platform/{{env}}/minio" {
  capabilities = ["read"]
}
path "secret/metadata/platform/{{env}}/minio" {
  capabilities = ["read", "list"]
}
# Required for the vault-agent healthcheck token lookup (AppRole auth)
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
