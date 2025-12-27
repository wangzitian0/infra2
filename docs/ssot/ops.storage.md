# 存储与备份 SSOT

> **SSOT Key**: `ops.storage`
> **核心定义**: 定义全系统的持久化策略 (StorageClass)、数据落盘位置及异地备份机制。

---

## 1. 真理来源 (The Source)

本话题的配置和状态由以下物理位置唯一确定：

| 维度 | 物理位置 (SSOT) | 说明 |
|------|----------------|------|
| **StorageClass** | [`bootstrap/4.storage.tf`](../../bootstrap/4.storage.tf) | K8s 存储类定义 |
| **备份归档** | **Cloudflare R2** | 异地冷备 |
| **本地快照** | **VPS `/data`** | 快速恢复快照 |

---

## 2. 架构模型

| 存储类型 | StorageClass | Reclaim Policy | 适用场景 |
|----------|--------------|----------------|----------|
| **Ephemeral** | `local-path` | Delete | 缓存 (Redis), 临时构建 |
| **Persistent** | `local-path-retain` | Retain | 数据库 (PG, CH, Vault) |
| **HostPath** | `hostpath` | Retain | Platform PG (L1) |

### 数据落盘目录

所有持久化数据最终映射到宿主机的 `/data` 目录：
- `/data/postgres` (L1)
- `/data/local-path-provisioner` (L2/L3 PVCs)
- `/data/backups` (Dump Files)

---

## 3. 设计约束 (Dos & Don'ts)

### ✅ 推荐模式 (Whitelist)

- **模式 A**: 有状态服务**必须**使用 `local-path-retain`，防止 Helm Uninstall 误删数据。
- **模式 B**: 必须配置定期 CronJob 将 `/data/backups` 同步到 R2。

### ⛔ 禁止模式 (Blacklist)

- **反模式 A**: **禁止** 直接在 Pod 中使用 `emptyDir` 存储重要数据。
- **反模式 B**: **严禁** 手动删除 `/data` 下的文件，除非你清楚后果。

---

## 4. 标准操作程序 (Playbooks)

### SOP-001: 扩容 PVC

- **触发条件**: 数据库磁盘已满
- **步骤**:
    1. 修改 Terraform/Helm 中的 `persistence.size`。
    2. Apply 变更。
    3. (对于不支持在线扩容的 StorageClass) 可能需要手动迁移数据。

### SOP-002: 恢复误删的 PVC

- **触发条件**: Helm Release 被删除，但数据还在 (Retain)
- **步骤**:
    1. 找到残留的 PV: `kubectl get pv`
    2. 移除 PV 的 ClaimRef: `kubectl patch pv <pv-name> -p '{"spec":{"claimRef": null}}'`
    3. 重新部署应用，手动绑定该 PV。

---

## 5. 验证与测试 (The Proof)

| 行为描述 | 测试文件 (Test Anchor) | 覆盖率 |
|----------|-----------------------|--------|
| **StorageClass 定义** | [`test_storage.py`](../../e2e_regressions/tests/bootstrap/storage_layer/test_storage.py) | ✅ Critical |

---

## Used by

- [docs/ssot/README.md](./README.md)
- [bootstrap/README.md](../../bootstrap/README.md)