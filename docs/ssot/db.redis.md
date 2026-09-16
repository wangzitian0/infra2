# Redis SSOT

> **SSOT Key**: `db.redis`
> **核心定义**: 平台共享 Redis 的配置、连接与安全规范。

---

## 1. 真理来源 (The Source)

| 维度 | 物理位置 (SSOT) | 说明 |
|------|----------------|------|
| **实例定义** | [`platform/02.redis/compose.yaml`](https://github.com/wangzitian0/infra2/blob/main/platform/02.redis/compose.yaml) | 服务配置 |
| **部署任务** | [`platform/02.redis/deploy.py`](https://github.com/wangzitian0/infra2/blob/main/platform/02.redis/deploy.py) | Invoke 任务 |
| **运行时密钥** | **Vault** (`secret/platform/<env>/redis`) | password |

---

## 2. 关键参数

| 项目 | 值 |
|------|-----|
| **数据目录** | `${DATA_PATH}` |
| **容器名** | `platform-redis${ENV_SUFFIX}` |
| **端口** | `6379` |
| **环境变量** | `REDIS_PASSWORD` |

---

## 3. 标准操作程序 (Playbooks)

### SOP-001: 部署/升级

```bash
python -m tools.deploy_v2 --service platform/redis --type staging --iac-ref vX.Y.Z --domain zitian.party
```

重建 redis 后,依赖方(声明了 `restart_after` 的 OpenPanel api/worker、Authentik worker)由 redis 的 sync 自动重启
(#726,见 [ops.pipeline.md](./ops.pipeline.md) "重建依赖 ⇒ 重启依赖方")。**手工**重建/重启 redis(`docker restart`、
Dokploy UI)不经过该路径,须同时重启它们:

```bash
docker restart platform-authentik-worker platform-openpanel-api platform-openpanel-worker   # production
docker restart platform-authentik-worker-staging                                            # staging
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
| **重建后重启依赖方,跳过时不重启**(#726) | `libs/tests/test_deploy_in_service.py`, `libs/tests/test_service_registry.py` | ✅ |

---

## Used by

- [docs/ssot/db.overview.md](./db.overview.md)
