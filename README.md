# infra2

基础设施自动化工作区：平台引导、服务部署、文档管理。

## 🚀 Quick Start

```bash
# 1. 配置环境变量
cp .env.example .env
# 编辑 .env 填入 VPS_HOST, INTERNAL_DOMAIN

# 2. 验证环境
uv run invoke check-env

# 3. 查看可用命令
uv run invoke --list
```

## 📁 项目结构

```
infra2/
├── bootstrap/        # L1 - 基础层 (1Password, Vault)
├── platform/         # L2 - 平台层 (PostgreSQL, Redis, Authentik)
├── e2e_regressions/  # E2E 测试
├── libs/             # 共享库 (Deployer, Config)
├── tools/            # 工具脚本 (env_sync)
└── docs/             # 文档
    ├── ssot/         # SSOT 真理源
    └── project/      # 项目追踪
```

## 🔧 环境变量体系

三层结构对应 Dokploy Project/Environment/Service：

| 层级 | 文件位置 | 说明 |
|------|----------|------|
| **Global** | `.env` | 全局 (VPS_HOST, INTERNAL_DOMAIN) |
| **Project** | `{project}/.env` | 项目级 |
| **Environment** | `{project}/.env.{env}` | 环境级 (production/staging) |
| **Service** | `{project}/{service}/.env.{env}` | 服务级 |

> 详见 [docs/env_management.md](docs/env_management.md)

## 📚 文档入口

| 入口 | 路径 | 用途 |
|------|------|------|
| **SSOT 索引** | [docs/ssot/README.md](docs/ssot/README.md) | 技术真理源 |
| **项目追踪** | [docs/project/README.md](docs/project/README.md) | 任务管理 |
| **AI 行为准则** | [AGENTS.md](AGENTS.md) | AI 协作规范 |

## 🛠️ 常用命令

```bash
# 部署服务
invoke postgres.setup
invoke redis.setup
invoke authentik.setup

# 环境变量管理
invoke env.status --project=platform --service=postgres
invoke env.push --project=platform --service=postgres
invoke env.pull --project=platform --service=postgres

# 健康检查
invoke postgres.shared.status
invoke redis.shared.status
```

## 🔗 相关链接

- 📖 Documentation: https://wangzitian0.github.io/infra2/
- 🔑 Secrets: 1Password (`infra2` vault)
- 🌐 Dokploy: `https://cloud.{INTERNAL_DOMAIN}`
