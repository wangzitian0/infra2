# Vault

HashiCorp Vault - 秘密管理和加密服务。

## 配置文件

- [`compose.yaml`](./compose.yaml) - Docker Compose 配置
- [`vault.hcl`](./vault.hcl) - Vault 配置文件

## 操作步骤

### 自动化任务 (Invoke)

```bash
invoke vault.setup
invoke vault.prepare
invoke vault.deploy
invoke vault.init
invoke vault.unseal
invoke vault.status
invoke vault.setup-tokens
```

> CLI 输出统一使用 `libs.console`，避免直接 `print`。
> `vault.setup-tokens` 默认只输出掩码 token；如需完整 token 用于录入 1Password，先设置 `VAULT_SHOW_TOKENS=1`。

### 1. 准备配置

```bash
ssh ${VPS_SSH_USER:-root}@<VPS_IP>
mkdir -p /data/bootstrap/vault/{file,logs,config}
chown -R 1000:1000 /data/bootstrap/vault
chmod 755 /data/bootstrap/vault
```

### 2. 上传配置文件

```bash
scp bootstrap/05.vault/vault.hcl ${VPS_SSH_USER:-root}@<VPS_IP>:/data/bootstrap/vault/config/
```

### 3. 在 Dokploy 部署

- 访问 https://cloud.$INTERNAL_DOMAIN
- 创建 Docker Compose 应用: vault
- Repository: GitHub → wangzitian0/infra2
- Compose Path: `bootstrap/05.vault/compose.yaml`

### 4. 初始化 Vault

```bash
export VAULT_ADDR=https://vault.$INTERNAL_DOMAIN

# 初始化（首次运行）
vault operator init

# ⚠️ 保存输出的 5 个 unseal keys 和 root token！

# Unseal（每次重启后需要至少 3 个 key）
vault operator unseal <key1>
vault operator unseal <key2>
vault operator unseal <key3>

# 登录
vault login <root-token>
```

### 5. 生成服务 Token（Vault-Init）

```bash
export VAULT_ROOT_TOKEN=<root-token>
invoke vault.setup-tokens
```

说明：
- `VAULT_ROOT_TOKEN` 从 1Password `op://Infra2/bootstrap-vault/Root Token` 获取。
- `vault.setup-tokens` 会自动为服务生成只读 token，并尝试写入 Dokploy 环境变量 `VAULT_APP_TOKEN`。

## 配置说明

### vault.hcl

- **storage**: 文件存储后端（/vault/file）
- **listener**: 监听 0.0.0.0:8200，TLS 由 Traefik 提供
- **ui**: Web UI 启用

### 生产环境 vs Dev 模式

| 特性 | 生产模式 | Dev 模式 |
|------|---------|---------|
| 数据持久化 | ✅ 文件 | ❌ 内存 |
| Unseal | ✅ 手动 | ❌ 自动 |
| Root Token | 随机 | 固定 |

## 安全建议

1. **Unseal Keys**: 分散存储于 1Password，至少 3 个才能 unseal
2. **Audit Log**: `vault audit enable file file_path=/vault/logs/audit.log`
3. **Auto-unseal**: 可配置使用 1Password Connect 自动 unseal
