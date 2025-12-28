# Redis SSOT

> **SSOT Key**: `db.redis`
> **核心定义**: 平台共享 Redis 的配置、连接与安全规范。

---

## 1. 真理来源 (The Source)

| 维度 | 物理位置 (SSOT) | 说明 |
|------|----------------|------|
| **实例定义** | [`platform/02.redis/compose.yaml`](https://github.com/wangzitian0/infra2/blob/main/platform/02.redis/compose.yaml) | 服务配置 |
| **部署任务** | [`platform/02.redis/deploy.py`](https://github.com/wangzitian0/infra2/blob/main/platform/02.redis/deploy.py) | Invoke 任务 |
| **运行时密钥** | **Vault** (`secret/platform/production/redis`) | password |

---

## 2. 关键参数

| 项目 | 值 |
|------|-----|
| **数据目录** | `/data/platform/redis` |
| **容器名** | `platform-redis` |
| **端口** | `6379` |
| **环境变量** | `REDIS_PASSWORD` |

---

## 3. 标准操作程序 (Playbooks)

### SOP-001: 部署/升级

```bash
invoke redis.setup
```

### SOP-002: 查看状态

```bash
invoke redis.shared.status
```

---

## 4. 验证与测试 (The Proof)

| 行为描述 | 验证方式 | 状态 |
|----------|----------|------|
| **服务可达** | `invoke redis.shared.status` | ✅ Manual |

---

## Used by

- [docs/ssot/db.overview.md](./db.overview.md)
