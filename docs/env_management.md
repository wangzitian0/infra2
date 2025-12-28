# Environment Variable Management

三层环境变量管理系统（对应 Dokploy Project/Environment/Service）。

## 三层结构

| Dokploy 层级 | 开发机文件 | Vault |
|--------------|------------|-------|
| **Project** | `{project}/.env` | `secret/{project}/` |
| **Environment** | `{project}/.env.{env}` | `secret/{project}/{env}/` |
| **Service** | `{project}/{service}/.env.{env}` | `secret/{project}/{env}/{service}/` |

优先级：`service > environment > project`

## 文件结构

```
infra2/
├── .env                              # 全局
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
invoke env.status --project=platform --service=postgres
invoke env.push --project=platform --service=postgres
invoke env.pull --project=platform --service=postgres
```

## Python 使用

```python
from libs.config import Config

config = Config(project='platform', env='production', service='postgres')
password = config.get('POSTGRES_PASSWORD')
```
