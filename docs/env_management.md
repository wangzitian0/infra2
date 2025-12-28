# Environment Variable Management

三层环境变量管理系统（对应 Dokploy Project/Environment/Service）。

> **详细文档**: [bootstrap.vars_and_secrets.md](./ssot/bootstrap.vars_and_secrets.md)

## SSOT 来源（按项目区分）

| 项目 | 环境变量 SSOT | 密钥 SSOT |
|-----|--------------|-----------|
| **platform** | Dokploy | Vault |
| **bootstrap** | 1Password | 1Password |

## 三层结构

| Dokploy 层级 | 开发机文件 | Vault (platform) |
|--------------|------------|------------------|
| **Project** | `{project}/.env` | `secret/{project}/` |
| **Environment** | `{project}/.env.{env}` | `secret/{project}/{env}/` |
| **Service** | `{project}/{service}/.env.{env}` | `secret/{project}/{env}/{service}/` |

优先级：`service > environment > project`

## 文件结构

```
infra2/
├── bootstrap/
│   ├── .env                          # project
│   ├── .env.production               # environment
│   ├── 04.1password/.env.production  # service
│   └── 05.vault/.env.production      # service
├── platform/
│   ├── .env                          # project
│   ├── .env.production               # environment
│   ├── 01.postgres/.env.production   # service
│   ├── 02.redis/.env.production      # service
│   └── 10.authentik/.env.production  # service
```

## 命令

```bash
# 查看配置状态
invoke env.status --project=platform --service=postgres

# 拉取配置（platform 从 Vault，bootstrap 从 1Password）
invoke env.pull --project=platform --service=postgres
invoke env.pull --project=bootstrap --service=vault

# 推送配置
invoke env.push --project=platform --service=postgres
invoke env.push --project=bootstrap --service=vault
```

## Python 使用

```python
from libs.config import Config

config = Config(project='platform', env='production', service='postgres')
password = config.get('POSTGRES_PASSWORD')
```
