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
invoke vault.setup-approle
```

> CLI 输出统一使用 `libs.console`，避免直接 `print`。
> `vault.setup-approle` 为每个服务创建 `{project, env, service}` 作用域的 AppRole，把 `role_id`/`secret_id`（`secret_id_ttl=0` 永不过期）注入 Dokploy env，不打印任何 secret。
> Generated app tokens are scoped by `{project, env, service}` and previous
> tracked accessors are revoked after successful Dokploy update.

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

### 4. 注入服务认证凭证（AppRole，Vault-Init）

服务运行时通过 **AppRole** 认证 Vault：`invoke vault.setup-approle` 为每个服务生成
`role_id` + `secret_id`，并注入 Dokploy env `VAULT_ROLE_ID`/`VAULT_SECRET_ID`。

```bash
export VAULT_ROOT_TOKEN=$(op read 'op://Infra2/dexluuvzg5paff3cltmtnlnosm/Root Token') # item: bootstrap/vault/Root Token
# 全部服务（按 deploy.py 注册派生）
invoke vault.setup-approle --deploy
# 单个服务 / staging 修复
DEPLOY_ENV=staging invoke vault.setup-approle --project=finance_report --service=app --deploy
```

说明：
- `vault.setup-approle` 为每个 `{project, env, service}` 写策略 + approle role，取 `role_id`/`secret_id`
  并自动注入 Dokploy（`--deploy`），随后等待运行时部署记录。
- vault-agent 用 approle 自身续期，无需周期 token 续命。
- AppRole 凭证缺失会在部署期 **fail-closed**（`libs.deployer._assert_approle_creds_present` 检查
  `VAULT_ROLE_ID`/`VAULT_SECRET_ID`/`VAULT_ADDR`），避免 vault-agent 起不来导致服务卡死。

> **IaC Runner**：也已迁到 AppRole（#369，完成 #257/#259）。它额外用一枚**有界、非 root**的
> deployer token（每次部署 `auth/approle/login` 现签）传给 `.sync` 子进程读写跨服务 secret，
> 设计有 P0 反环依赖约束，见 `docs/ssot/bootstrap.iac_runner.md` §6.4。

### 5. 应用接入 Vault（vault-init）

**目标**：运行时从 Vault 拉取密钥，不在磁盘持久化。

流程：
1. 在 Vault 写入 `secret/data/<project>/<env>/<service>`（KV v2）。
2. 运行 `DEPLOY_ENV=<env> invoke vault.setup-approle --project=<project> --service=<service> --deploy`
   为服务生成 approle role 并注入 `VAULT_ROLE_ID`/`VAULT_SECRET_ID`。
3. Compose 加 `vault-agent` sidecar（approle 模板见 `docs/onboarding/07.new-service-sop.md`），拉取 Vault 并写入 `/vault/secrets/.env`。
4. 应用容器通过 `env_file` 或启动脚本读取渲染出的 `.env`。

要点：
- `VAULT_ADDR` 放在 Dokploy 项目级 env（非敏感，但 **必须存在** —— 缺失会让 vault-agent 卡住，部署期已 fail-closed）。
- `VAULT_ROLE_ID`/`VAULT_SECRET_ID` 必须是 per-project/per-env/per-service 的 approle 凭证。
- `/vault/secrets` 使用 `tmpfs`，避免落盘。

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

### Reboot Health States

```bash
ssh ${VPS_SSH_USER:-root}@<VPS_IP> 'docker ps --filter name=vault --format "table {{.Names}}\t{{.Status}}"'
curl -fsS https://vault.$INTERNAL_DOMAIN/v1/sys/health
```

Expected healthy state after reboot:

- `vault`: `Up ... (healthy)`
- `vault-unsealer`: `Up ... (healthy)`
- Vault health endpoint returns HTTP 200.

`vault-unsealer` health is intentionally stricter than raw Connect `/health`.
It first performs the authenticated item lookup to initialize Connect sync, then
verifies:

- `OP_CONNECT_TOKEN`, `OP_VAULT_ID`, and `OP_ITEM_ID` are present.
- The configured bearer token can read `/v1/vaults/$OP_VAULT_ID/items/$OP_ITEM_ID`.
- 1Password Connect dependencies are active (`sqlite`, `sync`, `1Password`).
- Vault is initialized, unsealed, and returns an accepted health status.

Degraded states:

- `vault` is `unhealthy`: Vault is not initialized, is sealed, or cannot answer `vault status`.
- `vault-unsealer` is `unhealthy`: Vault is sealed, 1Password Connect is unreachable, Connect sync is not active, the Connect token is stale/unauthorized, or required unseal-key environment variables are missing.
- `vault-unsealer` is restarting: check `OP_CONNECT_TOKEN`, `OP_VAULT_ID`, and `OP_ITEM_ID`.

Failed states that need immediate action:

- Vault remains HTTP 503 after `vault-unsealer` has run for more than one healthcheck window.
- 1Password Connect `/health` is not HTTP 200.
- 1Password Connect `/health` is HTTP 200 but reports `sync: TOKEN_NEEDED` or `1Password: UNINITIALIZED`.
- `vault-unsealer` logs show HTTP 401/403 from 1Password Connect authenticated item lookup.
- `docker logs vault-unsealer --tail 100` shows missing unseal keys or 1Password authorization failures.

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
