# Environment Variable Management

无本地存储，远端优先的三层环境变量管理系统。

> **详细文档**: [bootstrap.vars_and_secrets.md](./ssot/bootstrap.vars_and_secrets.md)

## SSOT 来源

| 项目 | 环境变量 SSOT | 密钥 SSOT |
|-----|--------------|-----------|
| `bootstrap` | 1Password | 1Password |
| `platform` | Dokploy | Vault |

## 三层结构

| 层级 | 路径格式 |
|------|----------|
| **Project** | `{project}` |
| **Environment** | `{project}/{env}` |
| **Service** | `{project}/{env}/{service}` |

## 命令

```bash
# 读写环境变量
invoke env.get KEY --project=platform --env=production
invoke env.set KEY=VALUE --project=platform --env=production

# 读写密钥
invoke env.secret-get KEY --project=platform --env=production
invoke env.secret-set KEY=VALUE --project=platform --env=production

# 预览
invoke env.preview --project=platform --env=production --service=postgres

# 复制
invoke env.copy --from-project=platform --from-env=staging --to-env=production
```

## Python API

```python
from libs.config import Config

config = Config(project='platform', env='production', service='postgres')
host = config.get('POSTGRES_HOST')
password = config.get_secret('POSTGRES_PASSWORD')
```
