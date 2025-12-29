# infra2

基础设施自动化工作区：平台引导、服务部署、文档管理。

## 🚀 Quick Start

```bash
# 前置：安装 1Password CLI 与 uv
# macOS: brew install 1password-cli
# uv: curl -LsSf https://astral.sh/uv/install.sh | sh

# 1. 登录 1Password CLI（首次）
op signin

# 2. 验证 init/env_vars (VPS_HOST, INTERNAL_DOMAIN)
uv run invoke local.bootstrap

# 3. 验证环境
uv run invoke check-env

# 4. 查看可用命令
uv run invoke --list
```

## 📁 项目结构

```
infra2/
├── bootstrap/        # L1 - 基础层 (1Password, Vault)
├── platform/         # L2 - 平台层 (PostgreSQL, Redis, Authentik)
├── e2e_regressions/  # E2E 测试
├── libs/             # 共享库 (Deployer, Config)
├── tools/            # 工具脚本 (env_tool)
└── docs/             # 文档
    ├── ssot/         # SSOT 真理源
    └── project/      # 项目追踪
```

## 🔧 环境变量体系

三层结构对应 Dokploy Project/Environment/Service，**无本地存储**，远端优先。

| 项目 | 环境变量 SSOT | 密钥 SSOT |
|-----|--------------|-----------|
| `bootstrap` | 1Password | 1Password |
| `platform` | Dokploy | Vault |

> 详见 [docs/ssot/bootstrap.vars_and_secrets.md](docs/ssot/bootstrap.vars_and_secrets.md)

## 📚 文档入口

| 入口 | 路径 | 用途 |
|------|------|------|
| **SSOT 索引** | [docs/ssot/README.md](docs/ssot/README.md) | 技术真理源 |
| **项目追踪** | [docs/project/README.md](docs/project/README.md) | 任务管理 |
| **AI 行为准则** | [AGENTS.md](AGENTS.md) | AI 协作规范 |

## 🛠️ 常用命令

### 环境变量管理 (env_tool)

| 命令 | 说明 |
|------|------|
| `invoke env.get KEY --project=P --env=E --service=S` | 读取环境变量 |
| `invoke env.set KEY=VAL --project=P --env=E --service=S` | 写入环境变量 |
| `invoke env.secret-get KEY --project=P --env=E` | 读取密钥 |
| `invoke env.secret-set KEY=VAL --project=P --env=E` | 写入密钥 |
| `invoke env.preview --project=P --env=E --service=S` | 预览所有变量 |
| `invoke env.copy --from-project=P --from-env=E1 --to-env=E2` | 复制环境配置 |

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
