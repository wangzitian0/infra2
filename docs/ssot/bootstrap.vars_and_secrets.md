# 变量与密钥管理 SSOT

> **SSOT Key**: `bootstrap.vars_and_secrets`
> **核心定义**: 三层环境变量体系，从 1Password 到本地 `.env` 的流转。

---

## 1. 真理来源 (The Source)

| 维度 | 物理位置 | 说明 |
|------|----------|------|
| **Master Record** | 1Password | 所有密钥的源头 |
| **Project 级** | `.env` | 全局共享 |
| **Environment 级** | `.env.<env>` | 环境配置 |
| **Service 级** | `{project}/.env.<env>` | 项目专属 |

---

## 2. 三层结构

| 层级 | 文件 | Vault | Dokploy |
|------|------|-------|---------|
| **Project** | `.env` | `secret/{project}/` | Project Variables |
| **Environment** | `.env.<env>` | `secret/{project}/{env}/` | Environment Variables |
| **Service** | `{project}/.env.<env>` | `secret/{project}/{env}/service/` | Service Variables |

---

## 3. 文件结构

```
infra2/
├── .env                    # Project (INTERNAL_DOMAIN, VPS_HOST)
├── .env.production         # Environment (production)
├── .env.staging            # Environment (staging)
├── bootstrap/
│   └── .env.production     # bootstrap service
├── platform/
│   └── .env.production     # platform service
└── e2e_regression/
    └── .env.production     # e2e service
```

---

## 4. 设计约束

### ✅ 推荐模式
- 从 `.env.example` 复制到 `.env`，填入实际值
- 密钥从 1Password 获取

### ⛔ 禁止模式
- **严禁** 将 `.env` 提交到 Git
- **禁止** 在代码中硬编码密钥

---

## 5. 命令

```bash
invoke env.status --project=platform
invoke env.push --project=platform --level=service
invoke env.pull --project=platform --level=service
```

---

## Used by

- [docs/ssot/README.md](./README.md)
