# Environment Variable Management

三层环境变量管理系统。

## 三层结构

| 层级 | 开发机 | Vault | Dokploy |
|------|--------|-------|---------|
| **Project** | `.env` | `secret/{project}/` | Project Variables |
| **Environment** | `.env.<env>` | `secret/{project}/{env}/` | Environment Variables |
| **Service** | `{project}/.env.<env>` | `secret/{project}/{env}/service/` | Service Variables |

优先级：`service > environment > project`

## 文件结构

```
infra2/
├── .env                    # Project 级 (INTERNAL_DOMAIN, VPS_HOST)
├── .env.production         # Environment 级 (production)
├── .env.staging            # Environment 级 (staging)
├── bootstrap/
│   └── .env.production     # bootstrap 的 service 级
├── platform/
│   └── .env.production     # platform 的 service 级
└── e2e_regression/
    └── .env.production     # e2e 的 service 级
```

## Vault 路径

```
secret/data/
└── platform/               # project
    └── production/         # environment
        └── service/        # service
            └── POSTGRES_PASSWORD
```

## 命令

```bash
# 查看配置
invoke env.status --project=platform

# 本地 → Vault
invoke env.push --project=platform --level=service

# Vault → 本地
invoke env.pull --project=platform --level=service
```

## Python 使用

```python
from libs.config import Config

config = Config(project='platform', env='production')
password = config.get('POSTGRES_PASSWORD')
domain = config.get('INTERNAL_DOMAIN', level='project')
```
