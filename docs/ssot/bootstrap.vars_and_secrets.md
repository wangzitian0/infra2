# 变量与密钥管理 SSOT

> **SSOT Key**: `bootstrap.vars_and_secrets`
> **核心定义**: 三层环境变量体系（对应 Dokploy Project/Environment/Service），无本地存储，远端优先。

---

## 1. SSOT 来源（按项目区分）

> [!IMPORTANT]
> **本地不存储**环境变量和密钥，直接从远端读写。

| 项目 | 环境变量 SSOT | 密钥 SSOT |
|-----|--------------|-----------|
| `bootstrap` | 1Password | 1Password |
| `platform` | Dokploy | Vault |

---

## 2. 三层结构 (对应 Dokploy)

| Dokploy 层级 | 路径格式 | Vault 路径 (platform) |
|--------------|----------|----------------------|
| **Project** | `{project}` | `secret/{project}/` |
| **Environment** | `{project}/{env}` | `secret/{project}/{env}/` |
| **Service** | `{project}/{env}/{service}` | `secret/{project}/{env}/{service}/` |

### 本地文件（仅模板）

| 文件 | 内容 | Git 跟踪 |
|------|------|----------|
| `.env.example` | 仅 KEY（无 VALUE） | ✅ 进 Git |

---

## 3. 命令行工具

```bash
# 读取环境变量
invoke env.get KEY --project=platform --env=production --service=postgres

# 写入环境变量
invoke env.set KEY=VALUE --project=platform --env=production

# 读取密钥
invoke env.secret-get KEY --project=platform --env=production

# 写入密钥
invoke env.secret-set KEY=VALUE --project=platform --env=production

# 预览所有变量（不存储本地）
invoke env.preview --project=platform --env=production --service=postgres

# 复制环境配置
invoke env.copy --from-project=platform --from-env=staging --to-env=production
```

---

## 4. Python API

```python
from libs.config import Config

# 从远端加载（无本地存储）
config = Config(project='platform', env='production', service='postgres')

# 获取环境变量
host = config.get('POSTGRES_HOST')

# 获取密钥
password = config.get_secret('POSTGRES_PASSWORD')

# 获取全部
all_vars = config.all()
all_secrets = config.all_secrets()
```

---

## 5. 设计约束

### ✅ 推荐模式
- 使用 `invoke env.*` 命令读写远端
- 使用 `Config` 类在代码中获取配置
- `.env.example` 只保留 KEY

### ⛔ 禁止模式
- **禁止** 本地存储实际环境变量值
- **禁止** 在代码中硬编码密钥
- **禁止** 提交任何 `.env` 文件到 Git

---

## Used by

- [docs/ssot/README.md](./README.md)
- [docs/env_management.md](../env_management.md)
