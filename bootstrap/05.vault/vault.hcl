# Vault Configuration File
# Note: Currently overridden by VAULT_LOCAL_CONFIG in compose.yaml for better environment variable support.

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = 1
}

storage "file" {
  path = "/vault/file"
}

# api_addr is set via VAULT_LOCAL_CONFIG in compose.yaml
ui = true
disable_mlock = true
log_level = "info"
