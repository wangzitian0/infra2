# 变量与密钥管理 SSOT

> **SSOT Key**: `bootstrap.vars_and_secrets`
> **核心定义**: 三层环境变量体系（对应 Dokploy Project/Environment/Service），无本地存储，远端优先。

---

## 1. SSOT 来源（按凭证类型区分）

> [!IMPORTANT]
> **本地不存储**环境变量和密钥，直接从远端读写。

### 1.1 三种凭证类型 (Credential Types)

CLI 命令 `invoke env.*` 和 `get_secrets()` 函数通过 `--type` 参数区分凭证类型：

| Type | 存储后端 | 路径格式 | 用途 |
|------|----------|----------|------|
| `bootstrap` | 1Password | `{project}/{service}` | Bootstrap 项目的凭证（无 env 层） |
| `root_vars` | 1Password | `{project}/{env}/{service}` | 非 bootstrap 项目的 superadmin 密码 |
| `app_vars` | Vault | `secret/data/{project}/{env}/{service}` | 应用变量（**默认**，vault-agent 消费） |

**核心原则**：
- **不指定 `--type`** → 默认 `app_vars` → **Vault**（最安全）
- 要写 1Password 必须**显式声明** `--type=bootstrap` 或 `--type=root_vars`

### 1.2 按项目区分（简化视图）

| 项目 | 环境变量 SSOT | 密钥 SSOT |
|-----|--------------|-----------| 
| `bootstrap` | 1Password (`type=bootstrap`) | 1Password (`type=bootstrap`) |
| `platform` / 其他 | Dokploy | Vault (`type=app_vars`, 默认) |

### 1.3 密钥分类规则

**Web UI 密码** → 1Password（人工访问，`type=root_vars`）
- 用于浏览器登录、手动操作
- 例如：Dokploy admin 密码、Authentik admin 密码
- 存储在 1Password 便于 1Password 浏览器插件自动填充

**机器读密码** → Vault（自动化访问，`type=app_vars` 或省略）
- 用于服务间通信、API 调用、数据库连接
- 例如：数据库密码、Redis 密码、Service Token
- 存储在 Vault 便于 vault-agent 自动拉取

**Bootstrap 凭证** → 1Password（`type=bootstrap`）
- Bootstrap 阶段全部在 1Password（因为 Vault 尚未部署）
- 路径无 env 层：`{project}/{service}`


### 1.4 凭证同步方向（1Password ↔ Vault）

`root_vars`(1Password) 与 `app_vars`(Vault) 按**源头**决定同步方向，互不重叠、不形成双写环：

| 凭证类别 | 源头 (SSOT) | 同步方向 | 触发方式 | 示例 |
|---------|------------|---------|---------|------|
| **人配置的第三方凭证**（人去外部系统申请/填写） | 1Password (`root_vars`) | 1Password → Vault | 部署时**自动**（服务 `deploy.py` 的 `_sync_1password_to_vault`） | alerting 的飞书 `FEISHU_*`、桥接 basic-auth、心跳 token |
| **机器生成、人偶尔登录的 Web UI 密码** | Vault (`app_vars`，`generate_password`) | Vault → 1Password | **手动**（见 §3.5） | authentik / openpanel 的 admin `bootstrap_password` |
| **纯机器运行时 secret** | Vault (`app_vars`) | 不同步（仅 Vault） | — | DB / Redis 连接串、各服务 `secret_key` / `cookie_secret` |

**原则**：机器运行时**必须**读到的 → 自动同步进 Vault；人**偶尔**才用的（登录 / 灾难恢复）→ 手动同步或直接放 1Password。

> **为什么 iac-runner 需要 `OP_SERVICE_ACCOUNT_TOKEN`**
> iac-runner 自身读 Vault 走 AppRole（Dokploy 注入的 `VAULT_ROLE_ID` / `VAULT_SECRET_ID`，**不用** `op`）。它需要 `op` 的**唯一**原因是：它执行平台部署任务，其中"1Password → Vault 自动同步"这一步要用 `op` 读 1Password。因此 op 依赖**收敛到最小**——全仓只有 `platform/12.alerting` 一个服务真正触发此同步。**新增服务若没有"人配置的第三方凭证"，应直接 `env.set --type=app_vars` 写 Vault，不要引入 1Password 依赖。**

---

## 2. 1Password Vault 结构

Bootstrap 依赖 1Password CLI (`op`)，使用 **`Infra2`** vault 存储所有凭证。

### 2.1 Items 结构

| Item | 用途 | 分类 | 写入时机 |
|------|------|------|----------|
| `init/env_vars` | 种子变量（VPS_HOST, INTERNAL_DOMAIN） | 配置 | 初始化时手动创建 |
| `infra2.0 Credentials File` | Connect 凭证文件 | 机器读 | 从 1Password.com 下载 |
| `infra2.0 Access Token: infra2.0` | Connect API Token | 机器读 | 创建时自动生成 |
| `Service Account Auth Token: Infra2` | 1Password CLI Service Account Token | 机器读 | 供 `op` CLI 在 CI/IaC Runner 中免交互读取 Infra2 |
| `bootstrap/cloudflare` | Cloudflare DNS Token/Zone | 机器读 | 初始化时手动创建 |
| `bootstrap/vault/Root Token` | Vault root token | 机器读 | Vault 初始化时写入 |
| `bootstrap/vault/Unseal Keys` | Vault unseal keys (5 keys) | 机器读 | Vault 初始化时写入 |
| `bootstrap/dokploy/admin` | Dokploy Web UI 密码 | **Web UI** | 手动创建 |
| `platform/minio/admin` (`-staging` 可选) | MinIO Console 密码 | **Web UI** | 部署时写入 |
| `platform/authentik/admin` | Authentik Web UI 密码 | **Web UI** | 从 Vault 复制 |

> **注意**: `bootstrap/vault/Unseal Keys` 是 unsealer 自动解封 Vault 的关键，必须包含 `Unseal Key 1-5` 字段。

Token boundaries:

- `OP_SERVICE_ACCOUNT_TOKEN` is only for the 1Password CLI (`op`). It is consumed
  by local automation, CI, and IaC Runner subprocesses when they need to read
  Infra2 items.
- `OP_CONNECT_TOKEN` is only for 1Password Connect API clients. It is consumed by
  `vault-unsealer` and by the 1Password Connect deploy verifier. It must come
  from `infra2.0 Access Token: infra2.0` and match the
  deployed `1password-credentials.json`.
- `infra2.0 Credentials File` is mounted into the
  `op-connect-api` and `op-connect-sync` containers. The Connect containers do
  not consume `OP_SERVICE_ACCOUNT_TOKEN`.
- `bootstrap/vault/Root Token` is a Vault token. A successful Vault Web UI login
  and a successful Vault HTTP API token lookup are separate checks; automation
  that writes Vault KV requires a token with the needed API policy.

`bootstrap/cloudflare` 推荐字段：

- `CF_API_TOKEN`
- `CF_ZONE_ID`（可选，缺省时用 `CF_ZONE_NAME` 或 `INTERNAL_DOMAIN` 查找）
- `CF_ZONE_NAME`（可选）
- `CF_RECORDS`（可选，逗号分隔默认子域名列表）

### 2.2 创建 Infra2 Vault

```bash
# 首次设置时创建
op vault create "Infra2" --description "Infrastructure secrets"

# 创建种子变量 item
op item create --category=login --title="init/env_vars" --vault="Infra2" \
  "VPS_HOST[text]=<your_vps_ip>" \
  "VPS_SSH_USER[text]=root" \
  "INTERNAL_DOMAIN[text]=<your_domain>"
```

---

## 3. Bootstrap Phase

Bootstrap 不追求自动化，追求**可复现**。每个组件的 README 包含手动操作步骤。

### 3.1 Phase 检测

```bash
invoke local.phase   # 检测当前 bootstrap 阶段
invoke local.bootstrap  # 校验 1Password 的 init/env_vars（不生成本地 .env）
```

### 3.2 Phase 顺序

| Phase | 前置条件 | 操作 | 产出 |
|-------|---------|------|------|
| 0 | 1Password CLI 已登录 | `local.bootstrap` | 校验 init/env_vars（无本地文件） |
| 1 | VPS 可访问 | 安装 Dokploy | Dokploy Web UI |
| 2 | Dokploy 可用 | `1password.setup` | 1Password Connect |
| 3 | Connect 可用 | `vault.setup` | Vault 服务 |
| 4+ | Vault 可用 | platform 服务 | 生产服务 |

### 3.3 Vault 认证约定

运行时服务通过 **AppRole** 认证（`role_id` + `secret_id`）；旧的 `VAULT_APP_TOKEN` 周期 token
模型已全面退役（含 IaC Runner，#369；见 `docs/ssot/bootstrap.iac_runner.md` §6.4）。

| 变量名 | 权限 | 用途 | 存储位置 |
|--------|------|------|----------|
| `VAULT_ROOT_TOKEN` | Read + Write | `invoke vault.setup-approle` 生成/管理策略与 AppRole | 1Password `op://Infra2/dexluuvzg5paff3cltmtnlnosm/Root Token`（或 `/Token`；item: `bootstrap/vault/Root Token`） |
| `VAULT_ROLE_ID` + `VAULT_SECRET_ID` | AppRole 登录凭证 (per-project, per-env, per-service) | 运行时 vault-agent 用 approle 登录读取密钥 | Dokploy 服务环境变量（`setup-approle` 注入） |
| `VAULT_ADDR` | — | vault-agent 连接地址（非敏感，但**必须存在**，否则 agent 卡住） | Dokploy 项目级 env |

### 3.4 App 接入 Vault（vault-init）

**核心原则**：应用容器不直接持久化密钥，运行时由 `vault-agent` 读取 Vault 并写入 `tmpfs`。

步骤：
1. **准备 Vault 密钥**：写入 `secret/data/<project>/<env>/<service>`（KV v2）。
2. **生成 AppRole**：`export VAULT_ROOT_TOKEN=<token> && DEPLOY_ENV=<env> invoke vault.setup-approle --project=<project> --service=<service> --deploy`。
3. **注入运行时凭证**：`setup-approle --deploy` 自动写 Dokploy env `VAULT_ROLE_ID`/`VAULT_SECRET_ID` 并触发 redeploy。
4. **Compose 接入**：增加 `vault-agent` sidecar（approle 模板见 `docs/onboarding/07.new-service-sop.md`），读取 Vault 并渲染到 `/vault/secrets/.env`。
5. **应用读取**：主容器 entrypoint 中读取渲染出的 `.env`。

约束：
- `VAULT_ADDR` 仅是地址，可放在项目级 env（非敏感）；**必须存在**，缺失会让 vault-agent 卡死（部署期 fail-closed）。
- `VAULT_ROLE_ID`/`VAULT_SECRET_ID` 必须绑定 `{project, env, service}`，不能跨环境复用。
- `/vault/secrets` 需要挂载 `tmpfs`，避免磁盘落地。

### 3.5 Web UI 密码同步到 1Password

Platform 服务的 Web UI 密码（如 Authentik）虽然机器生成存储在 Vault，但需要同步到 1Password 供人工登录：

```bash
# 从 Vault 读取密码并写入 1Password
vault kv get -field=bootstrap_password secret/platform/<env>/authentik | \
  op item create --category=login --title="platform/authentik/admin" \
    --vault=Infra2 "username[text]=akadmin" "password[password]=-"
```

这样浏览器插件可以自动填充 Web UI 登录表单。

---

## 4. 三层结构 (对应 Dokploy)

| Dokploy 层级 | 路径格式 | Vault 路径 (platform, KV v2) |
|--------------|----------|----------------------|
| **Project** | `{project}` | `secret/data/{project}/` |
| **Environment** | `{project}/{env}` | `secret/data/{project}/{env}/` |
| **Service** | `{project}/{env}/{service}` | `secret/data/{project}/{env}/{service}/` |

### 命名与映射规则

- `project` / `env` / `service` **不允许包含** `-` 或 `/`，避免路径歧义。
- 1Password item 标题可以用 `project-env-service`，与 `{project}/{env}/{service}` **直接替换** `/` ↔ `-`（`-` 仅作为分隔符）。
- `op://` 路径建议使用 item ID，避免编码歧义：
  - `op read 'op://Infra2/dexluuvzg5paff3cltmtnlnosm/Root Token'`（item: `bootstrap/vault/Root Token`）
  - 若字段名是 `Token`，改为 `.../Token`

### 本地文件（仅模板）

| 文件 | 内容 | Git 跟踪 |
|------|------|----------|
| `.env.example` | 仅 KEY（无 VALUE，随组件分布） | ✅ 进 Git |
| `.env` | 可选本地种子变量（手动维护） | ❌ 不进 Git |

---

## 5. 命令行工具

```bash
# 读取密钥（默认 Vault）
invoke env.get KEY --project=<project> --env=<env> --service=<service>

# 写入密钥（默认 Vault）
invoke env.set KEY=VALUE --project=<project> --env=<env> --service=<service>

# 预览（masked，默认 Vault）
invoke env.list-all --project=<project> --service=<service>

# 查看 init/env_vars
invoke env.init-status
```

### 5.1 使用 `--type` 指定凭证类型

```bash
# Bootstrap 凭证（1Password，路径无 env）
invoke env.get KEY --project=bootstrap --service=vault --type=bootstrap
invoke env.set KEY=VALUE --project=bootstrap --service=vault --type=bootstrap

# Root vars（1Password，路径含 env）
invoke env.get ADMIN_PASSWORD --project=platform --env=production --service=authentik --type=root_vars
invoke env.set ADMIN_PASSWORD=secret --project=platform --env=production --service=authentik --type=root_vars

# App vars（Vault，默认行为）
invoke env.get POSTGRES_PASSWORD --project=platform --env=production --service=postgres
# 等价于:
invoke env.get POSTGRES_PASSWORD --project=platform --env=production --service=postgres --type=app_vars
```

> 省略 `--service` 表示读取/写入环境级（`{project}/{env}`）密钥。
> 省略 `--type` 默认走 Vault（`app_vars`）。

---

## 6. Python API

```python
from libs.env import OpSecrets, VaultSecrets, get_secrets, generate_password

# Bootstrap seed vars (init/env_vars) - 直接用 OpSecrets
init = OpSecrets()
seed = init.get_all()

# Bootstrap 凭证 (1Password, 无 env 层)
bootstrap_secrets = get_secrets(project='bootstrap', service='vault', type='bootstrap')
root_token = bootstrap_secrets.get('ROOT_TOKEN')

# Root vars (1Password, 含 env 层，用于 Web UI 密码)
root_vars = get_secrets(project='platform', env='production', service='authentik', type='root_vars')
admin_password = root_vars.get('ADMIN_PASSWORD')

# App vars (Vault, 默认行为)
secrets = get_secrets(project='platform', env='production', service='postgres')
# 等价于:
secrets = get_secrets(project='platform', env='production', service='postgres', type='app_vars')
password = secrets.get('POSTGRES_PASSWORD')

# 幂等生成密钥：不存在就生成并写入
if not password:
    password = generate_password(32)
    secrets.set('POSTGRES_PASSWORD', password)
```

### 6.1 Type 参数说明

| Type | 返回类型 | 路径格式 |
|------|----------|----------|
| `None` (默认) | `VaultSecrets` | `secret/data/{project}/{env}/{service}` |
| `'app_vars'` | `VaultSecrets` | `secret/data/{project}/{env}/{service}` |
| `'bootstrap'` | `OpSecrets` | `{project}/{service}` (无 env) |
| `'root_vars'` | `OpSecrets` | `{project}/{env}/{service}` |

---

## 7. 设计约束

### ✅ 推荐模式
- 使用 `invoke env.*` 命令读写远端
- 使用 `get_secrets()`/`OpSecrets` 在代码中获取配置
- **默认不指定 `--type`**，走 Vault（最安全）
- 需要 1Password 时**显式声明** `--type=bootstrap` 或 `--type=root_vars`
- `.env.example` 仅作为 KEY 清单（用于 `Deployer` 校验）
- 每个组件 README 包含完整手动步骤

### ⛔ 禁止模式
- **禁止** 本地存储实际环境变量值
- **禁止** 在代码中硬编码密钥
- **禁止** 提交任何 `.env` 文件到 Git

---

## Used by

- [docs/ssot/README.md](./README.md)
- [bootstrap/README.md](https://github.com/wangzitian0/infra2/blob/main/bootstrap/README.md)
