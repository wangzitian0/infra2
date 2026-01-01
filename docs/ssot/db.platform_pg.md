# Platform PostgreSQL SSOT

> **SSOT Key**: `db.platform_pg`
> **核心定义**: Platform 层的共享 PostgreSQL 定义与运维规范。

---

## 1. 真理来源 (The Source)

| 维度 | 物理位置 (SSOT) | 说明 |
|------|----------------|------|
| **实例定义** | [`platform/01.postgres/compose.yaml`](https://github.com/wangzitian0/infra2/blob/main/platform/01.postgres/compose.yaml) | 服务配置 |
| **部署任务** | [`platform/01.postgres/deploy.py`](https://github.com/wangzitian0/infra2/blob/main/platform/01.postgres/deploy.py) | Invoke 任务 |
| **运行时密钥** | **Vault** (`secret/platform/<env>/postgres`) | root_password |

---

## 2. 关键参数

| 项目 | 值 |
|------|-----|
| **数据目录** | `${DATA_PATH}` |
| **容器名** | `platform-postgres${ENV_SUFFIX}` |
| **端口** | `5432` |
| **环境变量** | `POSTGRES_PASSWORD`, `POSTGRES_USER` |

---

## 3. 标准操作程序 (Playbooks)

### SOP-001: 部署/升级

```bash
invoke postgres.setup
```

### SOP-002: 查看状态

```bash
invoke postgres.shared.status
```

---

## 4. 验证与测试 (The Proof)

| 行为描述 | 验证方式 | 状态 |
|----------|----------|------|
| **服务可达** | `invoke postgres.shared.status` | ✅ Manual |

---

## Used by

- [docs/ssot/db.overview.md](./db.overview.md)
