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

### 1. 部署 Vault

```bash
invoke vault.deploy
```

自动执行：
- 创建 `/data/bootstrap/vault/{file,logs,config}` 目录
- 上传 `vault.hcl` 配置
- 通过 Dokploy API 部署

### 2. 初始化 Vault

```bash
invoke vault.init
```

`init` 会先检查 Vault 是否可达，然后提示手动初始化步骤。

**方式一：Web UI 初始化（推荐）**

1. 访问 https://vault.$INTERNAL_DOMAIN
2. 选择初始化参数（默认 5 个 key，阈值 3）
3. 下载/保存 unseal keys 和 root token
4. 存入 1Password: `bootstrap/vault/Unseal Keys` 和 `bootstrap/vault/Root Token`

**方式二：CLI 初始化**

```bash
export VAULT_ADDR=https://vault.$INTERNAL_DOMAIN
vault operator init

# ⚠️ 保存输出的 5 个 unseal keys 和 root token！
```

### 3. Unseal Vault

每次 Vault 重启后需要至少 3 个 key 解封：

```bash
vault operator unseal <key1>
vault operator unseal <key2>
vault operator unseal <key3>
```

或通过 Web UI 解封。

### 4. 生成服务 Token（Vault-Init）

```bash
export VAULT_ROOT_TOKEN=$(op read 'op://Infra2/bootstrap%2Fvault%2FRoot%20Token/Root%20Token')
invoke vault.setup-tokens
```

说明：
- `vault.setup-tokens` 会自动为服务生成只读 token，并尝试写入 Dokploy 环境变量 `VAULT_APP_TOKEN`。

### 5. 应用接入 Vault（vault-init）

**目标**：运行时从 Vault 拉取密钥，不在磁盘持久化。

流程：
1. 在 Vault 写入 `secret/data/platform/production/<service>`（KV v2）。
2. 运行 `invoke vault.setup-tokens` 为服务生成只读 token。
3. Dokploy 服务环境变量设置 `VAULT_APP_TOKEN`（可自动注入）。
4. Compose 加 `vault-init` 容器，拉取 Vault 并写入 `/secrets/.env`。
5. 应用容器通过 `env_file: /secrets/.env` 或启动脚本 `source /secrets/.env` 读取。

要点：
- `VAULT_ADDR` 可放在 Dokploy 项目级 env。
- `VAULT_APP_TOKEN` 必须是 per-service 的只读 token。
- `/secrets` 使用 `tmpfs`，避免落盘。

## 常见问题 / Troubleshooting

### Dokploy GitHub 部署失败：repository not found

**症状**：`git clone` 失败，URL 显示 `owner/owner/repo`（owner 重复）

**原因**：Dokploy API 的 `repository` 和 `owner` 参数需要分开传递：
```python
# ❌ 错误
repository="wangzitian0/infra2"  # Dokploy 会再拼一次 owner

# ✅ 正确
repository="infra2"
owner="wangzitian0"
```

**解决**：已在代码中修复，确保 `repository` 只传 repo 名。

### vault.init 前检查

`invoke vault.init` 会先检查 Vault 是否可达（接受 501/503 状态码，表示服务运行但未初始化/已密封），避免在部署失败时继续执行初始化。

### Vault 健康端点状态码

| 状态码 | 含义 |
|--------|------|
| 200 | 已初始化，已解封，活跃 |
| 429 | 已解封，standby |
| 472 | recovery mode |
| 473 | performance standby |
| 501 | 未初始化 |
| 503 | 已密封 |

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
