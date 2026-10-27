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
| **备份归档** | **Google Drive (via rclone crypt)** | 异地冷备（off-host，rclone crypt 客户端加密）。清单由各服务 `deploy.py` 的 `BackupFacet` 声明派生（`common/backup/verification.py`，#542）；分级保留策略：周备保留 60 天（滑动窗口 ~8 周），每 3 个月季度长存快照保留 2 年（730 天）；验证见 [`ops.recovery.md`](./ops.recovery.md) SOP-004/005/006 |

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

### SOP-003: 宿主机残留回收站隔离与延迟清理 (Host Quarantine & 15-Day Delayed Purge)

- **触发条件**: 清理 VPS 宿主机历史残留目录、旧归档或退役脚本（#1373）
- **策略**: 采用两阶段“先隔离、后清除”机制，严禁在生产宿主机直接执行无缓冲的 `rm -rf`。
- **物理位置**: `/data/backups/quarantine/<batch-id>/files/<rel_path>`，索引清单位于 `/data/backups/quarantine/manifest.jsonl`（权限 `0600`，记录原始路径、SHA256、字节大小、`quarantined_at` 与 15 天后的 `purge_after_epoch`）。
- **步骤**:
    1. 只读扫描：`bootstrap/01.dokploy_install/host_guard/host_quarantine.sh --check`（零副作用列出残留项、权限漂移与退役 cron）。
    2. 迁入回收站：`host_quarantine.sh --stage`（先备份并注释退役 cron，再按原始相对路径 `mv` 至 `/data/backups/quarantine/<batch-id>/files/`，并将 `/tmp/F3CNN` 活跃锁目录收敛为 `0700`/`0600` 原地保留）。
    3. 观察期核查：`host_quarantine.sh --list`（保留 15 天观察期，期间若需回滚可直接按 `original_path` 迁回）。
    4. 到期物理清理：观察期满 15 天后执行 `host_quarantine.sh --purge-expired`（仅删除 `now_epoch >= purge_after_epoch` 的到期批次并更新清单状态为 `purged`）。

---

## 5. 验证与测试 (The Proof)

| 行为描述 | 验证方式 | 状态 |
|----------|----------|------|
| **存储路径一致性** | `compose.yaml` 中路径检查 | ✅ Manual |
| **宿主机残留回收站隔离与 15 天延迟清理** | `common/tests/test_host_quarantine_script.py` | ✅ Automated |

---

## Used by

- [docs/ssot/README.md](./README.md)
- [bootstrap/README.md](https://github.com/wangzitian0/infra2/blob/main/bootstrap/README.md)
