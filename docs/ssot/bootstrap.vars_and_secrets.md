# 变量与密钥管理 SSOT

> **SSOT Key**: `bootstrap.vars_and_secrets`
> **核心定义**: 三层环境变量体系，对应 Dokploy Project/Environment/Service。

---

## 1. 三层结构 (对应 Dokploy)

| Dokploy 层级 | Repo 文件 | 开发机文件 | Vault 路径 |
|--------------|-----------|------------|------------|
| **Global** | `.env.example` | `.env` | - |
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

## 2. 文件结构

```
infra2/
├── .env                              # 全局 (VPS_HOST, INTERNAL_DOMAIN)
├── .env.example                      # 模板
│
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

## 3. 测试环境变量

`e2e_regressions/` 的环境变量体系：

| 变量 | 说明 |
|------|------|
| `E2E_DOMAIN` | 测试域名 |
| `E2E_USERNAME` | 测试用户 |
| `E2E_PASSWORD` | 测试密码 |
| `HEADLESS` | 无头模式 (true/false) |

---

## 4. 优先级

`service > environment > project > global`

---

## 5. 命令

```bash
# 查看配置
invoke env.status --project=platform --service=postgres

# 本地 → Vault
invoke env.push --project=platform --service=postgres

# Vault → 本地
invoke env.pull --project=platform --service=postgres
```

---

## 6. 设计约束

### ✅ 推荐模式
- 从 `.env.*.example` 复制，填入实际值
- 密钥从 1Password 获取

### ⛔ 禁止模式
- **严禁** 将 `.env` / `.env.*` 提交到 Git（只提交 `.example`）
- **禁止** 在代码中硬编码密钥

---

## Used by

- [docs/ssot/README.md](./README.md)
- [docs/env_management.md](../env_management.md)
- [README.md](../../README.md)
