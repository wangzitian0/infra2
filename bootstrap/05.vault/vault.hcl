# Vault 配置文件
# 路径: /data/vault/config/vault.hcl

# 监听配置
listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = 1  # Traefik 已提供 HTTPS
}

# 存储后端（文件系统）
storage "file" {
  path = "/vault/file"
}

# API 地址
api_addr = "https://vault.${INTERNAL_DOMAIN}"

# UI 启用
ui = true

# 日志级别
log_level = "info"

# 禁用 mlock（容器环境中通常需要）
disable_mlock = false
