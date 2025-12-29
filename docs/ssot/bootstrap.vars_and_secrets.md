# 变量与密钥管理 SSOT

> **SSOT Key**: `bootstrap.vars_and_secrets`
> **核心定义**: 三层环境变量体系（对应 Dokploy Project/Environment/Service），无本地存储，远端优先。

---

## 1. SSOT 来源（按项目区分）

> [!IMPORTANT]
> **本地不存储**环境变量和密钥，直接从远端读写。

| 项目 | 环境变量 SSOT | 密钥 SSOT |
|-----|--------------|-----------| 
| `bootstrap` | 1Password | 1Password |
| `platform` | Dokploy | Vault |

---

## 2. 1Password Vault 结构

Bootstrap 依赖 1Password CLI (`op`)，使用 **`Infra2`** vault 存储所有凭证。

### 2.1 Items 结构

| Item | 用途 | 写入时机 |
|------|------|----------|
| `init/env_vars` | 种子变量（VPS_HOST, INTERNAL_DOMAIN） | 初始化时手动创建 |
| `bootstrap/1password/VPS-01 Credentials File` | Connect 凭证文件 | 从 1Password.com 下载 |
| `bootstrap/1password/VPS-01 Access Token: own_service` | Connect API Token | 创建时自动生成 |
| `bootstrap/vault/Unseal Keys` | Vault unseal keys + root token | Vault 初始化时写入 |

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
invoke local.bootstrap  # 校验 init/env_vars（不生成本地文件）
```

### 3.2 Phase 顺序

| Phase | 前置条件 | 操作 | 产出 |
|-------|---------|------|------|
| 0 | 1Password CLI 已登录 | `local.bootstrap` | init/env_vars 校验通过（无本地文件） |
| 1 | VPS 可访问 | 安装 Dokploy | Dokploy Web UI |
| 2 | Dokploy 可用 | `1password.setup` | 1Password Connect |
| 3 | Connect 可用 | `vault.setup` | Vault 服务 |
| 4+ | Vault 可用 | platform 服务 | 生产服务 |

---

## 4. 三层结构 (对应 Dokploy)

| Dokploy 层级 | 路径格式 | Vault 路径 (platform) |
|--------------|----------|----------------------|
| **Project** | `{project}` | `secret/{project}/` |
| **Environment** | `{project}/{env}` | `secret/{project}/{env}/` |
| **Service** | `{project}/{env}/{service}` | `secret/{project}/{env}/{service}/` |

### 本地文件（仅模板）

| 文件 | 内容 | Git 跟踪 |
|------|------|----------|
| `.env.example` | 仅 KEY（无 VALUE，随组件分布） | ✅ 进 Git |
| `.env` | bootstrap 种子变量（可选/手动） | ❌ 不进 Git |

---

## 5. 命令行工具

```bash
# 读取环境变量
invoke env.get KEY --project=platform --env=production --service=postgres

# 写入环境变量
invoke env.set KEY=VALUE --project=platform --env=production

# 读取密钥
invoke env.secret-get KEY --project=platform --env=production

# 写入密钥
invoke env.secret-set KEY=VALUE --project=platform --env=production

# 预览所有变量（不存储本地）
invoke env.preview --project=platform --env=production --service=postgres
```

---

## 6. Python API

```python
from libs.env import EnvManager, get_or_set

# 从远端加载（无本地存储）
mgr = EnvManager(project='platform', env='production', service='postgres')

# 获取环境变量
host = mgr.get_env('POSTGRES_HOST')

# 获取密钥
password = mgr.get_secret('POSTGRES_PASSWORD')

# 幂等生成密钥
# 如果远端已有，返回现有值；否则生成新值并写入
pw = get_or_set('POSTGRES_PASSWORD', length=32)
```

---

## 7. 设计约束

### ✅ 推荐模式
- 使用 `invoke env.*` 命令读写远端
- 使用 `EnvManager` 类在代码中获取配置
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
