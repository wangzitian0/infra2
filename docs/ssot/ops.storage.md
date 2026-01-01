# 存储与备份 SSOT

> **SSOT Key**: `ops.storage`
> **核心定义**: 定义全系统的持久化策略、数据落盘位置及异地备份机制。

---

## 1. 真理来源 (The Source)

| 维度 | 物理位置 (SSOT) | 说明 |
|------|----------------|------|
| **Postgres 数据目录** | `platform/01.postgres/compose.yaml` | `${DATA_PATH}` (e.g., `/data/platform/postgres${ENV_SUFFIX}`) |
| **Redis 数据目录** | `platform/02.redis/compose.yaml` | `${DATA_PATH}` (e.g., `/data/platform/redis${ENV_SUFFIX}`) |
| **Vault 数据目录** | `bootstrap/05.vault/compose.yaml` | `/data/bootstrap/vault` |
| **备份归档** | **Cloudflare R2** | 异地冷备（规划中） |

---

## 2. 架构模型

| 存储类型 | 位置 | 适用场景 |
|----------|------|----------|
| **Bootstrap** | `/data/bootstrap/<service>` | 1Password / Vault |
| **Platform** | `/data/platform/<service>${ENV_SUFFIX}` | Postgres / Redis / Authentik |

---

## 3. 设计约束 (Dos & Don'ts)

### ✅ 推荐模式 (Whitelist)

- **模式 A**: 所有有状态服务必须显式挂载到 `/data/...`。
- **模式 B**: 备份策略需在 SSOT 中注明计划窗口与保留策略。

### ⛔ 禁止模式 (Blacklist)

- **反模式 A**: **禁止** 将数据库数据落在容器临时目录。
- **反模式 B**: **严禁** 未备份直接删除 `/data`。

---

## 4. 标准操作程序 (Playbooks)

### SOP-001: 扩容磁盘

- **触发条件**: 磁盘使用率 > 80%
- **步骤**:
    1. 扩容 VPS 磁盘（云厂商操作）。
    2. 在宿主机扩展文件系统。
    3. 运行服务自检：`invoke postgres.shared.status` / `invoke redis.shared.status`。

### SOP-002: 迁移数据目录

- **触发条件**: 迁移到新磁盘或新路径
- **步骤**:
    1. 停止对应服务（Dokploy Stop）。
    2. 迁移 `/data/<layer>/<service>${ENV_SUFFIX}` 目录（非生产带后缀）。
    3. 启动服务并验证健康。

---

## 5. 验证与测试 (The Proof)

| 行为描述 | 验证方式 | 状态 |
|----------|----------|------|
| **存储路径一致性** | `compose.yaml` 中路径检查 | ✅ Manual |

---

## Used by

- [docs/ssot/README.md](./README.md)
- [bootstrap/README.md](https://github.com/wangzitian0/infra2/blob/main/bootstrap/README.md)
