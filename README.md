# infra2

基础设施自动化工作区：平台引导、服务部署、文档管理。

## 🚀 Quick Start

```bash
# 前置：安装 1Password CLI 与 uv
# macOS: brew install 1password-cli
# uv: curl -LsSf https://astral.sh/uv/install.sh | sh

# 1. 初始化本地依赖
invoke local.init

# 2. 登录 1Password CLI（首次）
op signin

# 3. 验证 init/env_vars (VPS_HOST, INTERNAL_DOMAIN)
invoke local.bootstrap

# 4. 验证环境
invoke check-env

# 5. 查看可用命令
invoke --list
```

## 🧭 CLI 运行方式

本文示例默认使用 `invoke`；若未激活虚拟环境，请使用 `uv run invoke` 代替。

## 📁 项目结构

```
infra2/
├── bootstrap/        # L1 - 基础层 (1Password, Vault)
├── platform/         # L2 - 平台层 (PostgreSQL, Redis, Authentik)
├── e2e_regressions/  # E2E 测试
├── libs/             # 共享库 (env, deployer, console)
├── tools/            # CLI 工具 (env, local)
└── docs/             # 文档
    ├── ssot/         # SSOT 真理源
    └── project/      # 项目追踪
```

## 📚 文档入口

| 入口 | 路径 | 用途 |
|------|------|------|
| **Onboarding** | [docs/onboarding/README.md](docs/onboarding/README.md) | 新手/应用接入 |
| **SSOT 索引** | [docs/ssot/README.md](docs/ssot/README.md) | 技术真理源 |
| **项目追踪** | [docs/project/README.md](docs/project/README.md) | 任务管理 |
| **AI 行为准则** | [AGENTS.md](AGENTS.md) | AI 协作规范 |
| **文档索引** | [docs/README.md](docs/README.md) | 文档总入口 |

## 🔧 环境变量体系

三层结构对应 Dokploy Project/Environment/Service，**无本地存储**，远端优先。

| 项目 | 环境变量 SSOT | 密钥 SSOT |
|-----|--------------|-----------|
| `bootstrap` | 1Password | 1Password |
| `platform` | Dokploy | Vault |

> 详见 [docs/ssot/bootstrap.vars_and_secrets.md](docs/ssot/bootstrap.vars_and_secrets.md) 与 [docs/ssot/platform.automation.md](docs/ssot/platform.automation.md)

## 🛠️ 常用命令

### 密钥管理 (env_tool)

| 命令 | 说明 |
|------|------|
| `invoke env.get KEY --project=<project> --env=<env> --service=<service>` | 读取密钥 |
| `invoke env.set KEY=VAL --project=<project> --env=<env> --service=<service>` | 写入密钥 |
| `invoke env.list-all --project=<project> --service=<service>` | 列出密钥（masked） |
| `invoke env.init-status` | 查看 init/env_vars |

> 省略 `--service` 表示读取/写入环境级（`{project}/{env}`）密钥。

### 本地工具 (local)

```bash
invoke local.check
invoke local.init
invoke local.bootstrap
invoke local.phase
invoke local.version
```

### 服务部署

```bash
invoke postgres.setup
invoke redis.setup
invoke authentik.setup
```

### 健康检查

```bash
invoke postgres.shared.status
invoke redis.shared.status
```

## 🔗 相关链接

- 📖 Documentation: https://wangzitian0.github.io/infra2/
- 🔑 Secrets: 1Password (`Infra2` vault)
- 🌐 Dokploy: `https://cloud.{INTERNAL_DOMAIN}`
