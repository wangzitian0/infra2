# 变量与密钥管理 SSOT

> **SSOT Key**: `bootstrap.vars_and_secrets`
> **核心定义**: 三层环境变量体系，对应 Dokploy Project/Environment/Service。不同项目有不同的 SSOT。

---

## 1. SSOT 来源（按项目区分）

> [!IMPORTANT]
> **不同项目有不同的 SSOT**，这是最重要的设计约束。

### Platform 项目

| 类型 | SSOT | 数据流向 |
|-----|------|----------|
| **环境变量** | **Dokploy** | Dokploy → Repo 文件 / 开发机本地文件 |
| **密钥** | **Vault** | Vault → Repo 文件 / 开发机本地文件 |

- 写入 Dokploy / Vault 通过脚本实现
- Repo 文件和开发机文件是**被动同步**的副本

### Bootstrap 项目

| 类型 | SSOT | 数据流向 |
|-----|------|----------|
| **环境变量** | **1Password (op CLI)** | 1Password → 开发机文件 / Dokploy |
| **密钥** | **1Password (op CLI)** | 1Password → 开发机文件 / Dokploy |

- 写入 1Password 通过 `op` 命令行工具
- 可同步到开发机文件或 Dokploy

---

## 2. 三层结构 (对应 Dokploy)

| Dokploy 层级 | Repo 文件 | 开发机文件 | Vault 路径 (platform only) |
|--------------|-----------|------------|----------------------------|
| **Project** | `{project}/.env.example` | `{project}/.env` | `secret/{project}/` |
| **Environment** | `{project}/.env.{env}.example` | `{project}/.env.{env}` | `secret/{project}/{env}/` |
| **Service** | `{project}/{service}/.env.{env}.example` | `{project}/{service}/.env.{env}` | `secret/{project}/{env}/{service}/` |

### 文件命名规范

| 后缀 | 说明 | Git 跟踪 |
|------|------|----------|
| `.example` | 模板文件 | ✅ 进 Git |
| 无后缀 | 实际配置（有敏感信息） | ❌ gitignore |
| `.local` | 仅开发机本地使用 | ❌ gitignore |

---

## 3. 文件结构

```
infra2/
├── bootstrap/
│   ├── .env                          # Project: bootstrap
│   ├── .env.example
│   ├── .env.production               # Environment: production
│   ├── .env.production.example
│   ├── 04.1password/
│   │   ├── .env.production           # Service: 1password
│   │   └── .env.production.example
│   └── 05.vault/
│       ├── .env.production
│       └── .env.production.example
│
├── platform/
│   ├── .env                          # Project: platform
│   ├── .env.example
│   ├── .env.production
│   ├── .env.production.example
│   ├── 01.postgres/
│   │   ├── .env.production
│   │   └── .env.production.example
│   ├── 02.redis/
│   │   └── ...
│   └── 10.authentik/
│       └── ...
│
└── e2e_regressions/
    ├── .env                          # 测试配置
    └── .env.example
```

---

## 4. 测试环境变量

`e2e_regressions/` 的环境变量体系：

| 变量 | 说明 |
|------|------|
| `E2E_DOMAIN` | 测试域名 |
| `E2E_USERNAME` | 测试用户 |
| `E2E_PASSWORD` | 测试密码 |
| `HEADLESS` | 无头模式 (true/false) |

---

## 5. 优先级

`service > environment > project`

---

## 6. 命令

### Platform 项目（环境变量从 Dokploy，密钥从 Vault）

```bash
# 查看配置状态
invoke env.status --project=platform --service=postgres

# 从 Vault 拉取密钥到本地
invoke env.pull --project=platform --service=postgres

# 推送本地密钥到 Vault
invoke env.push --project=platform --service=postgres
```

### Bootstrap 项目（从 1Password）

```bash
# 从 1Password 拉取到本地
invoke env.pull --project=bootstrap --service=vault

# 推送本地配置到 1Password
invoke env.push --project=bootstrap --service=vault
```

---

## 7. 设计约束

### ✅ 推荐模式
- 从 `.env.*.example` 复制，填入实际值
- **Platform 密钥**：从 Vault 获取
- **Bootstrap 配置**：从 1Password 获取

### ⛔ 禁止模式
- **严禁** 将 `.env` / `.env.*` 提交到 Git（只提交 `.example`）
- **禁止** 在代码中硬编码密钥
- **禁止** 混淆不同项目的 SSOT

---

## Used by

- [docs/ssot/README.md](./README.md)
- [docs/env_management.md](../env_management.md)
- [README.md](../../README.md)
