# Environment Variable Management

本文档描述三层环境变量管理系统的使用方法。

## 三层结构

| 层级 | 本地文件 | Vault 路径 | Dokploy |
|------|----------|------------|---------|
| Project | `.env` | `platform/` | Project Variables |
| Environment | `.env.{env}` | `platform/prod/` | Environment Variables |
| Service | `{service}/.env.{env}.local` | `platform/prod/postgres/` | Service Variables |

## 优先级

`service > environment > project`

## 文件结构

```
infra2/
├── .env                      # Project (INTERNAL_DOMAIN, VPS_HOST)
├── .env.prod                 # Prod environment
├── .env.staging              # Staging environment
└── platform/
    ├── 01.postgres/
    │   ├── .env.example      # Key 模板
    │   └── .env.prod.local   # Prod 值
    ├── 02.redis/
    │   └── ...
    └── 10.authentik/
        └── ...
```

## Vault 路径

```
secret/data/
└── platform/
    ├── INTERNAL_DOMAIN       # project 级
    ├── prod/
    │   ├── LOG_LEVEL         # environment 级
    │   ├── postgres/
    │   │   └── root_password # service 级
    │   ├── redis/
    │   │   └── password
    │   └── authentik/
    │       └── secret_key
    └── staging/
        └── ...
```

## 命令

```bash
# 查看配置
invoke env.status
invoke env.status --service=postgres

# 本地 → Vault
invoke env.push --level=service --env=prod --service=postgres

# Vault → 本地
invoke env.pull --level=service --env=prod --service=postgres
```

## Python 使用

```python
from tools.config import Config

config = Config(project='platform', env='prod', service='postgres')

# 自动优先级查找
password = config.get('POSTGRES_PASSWORD')

# 指定层级
domain = config.get('INTERNAL_DOMAIN', level='project')
```

## 部署流程

```bash
# 1. 部署 postgres (生成密码存入 Vault)
invoke postgres.setup

# 2. 部署 redis
invoke redis.setup

# 3. 部署 authentik (从 Vault 读取 pg/redis 密码)
invoke authentik.setup
```
