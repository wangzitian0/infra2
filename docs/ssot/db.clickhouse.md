# ClickHouse SSOT

> **SSOT Key**: `db.clickhouse`
> **核心定义**: ClickHouse (SigNoz Storage).

---

## 1. 现状

- **部署位置**: `platform/03.clickhouse`
- **用途**: SigNoz 可观测性平台的存储后端 (Logs, Metrics, Traces)。
- **架构**: 单节点 ClickHouse + ZooKeeper (协调)。
- **依赖**: 仅供 SigNoz 使用，无业务层直接依赖。

---

## 2. 验证与测试 (The Proof)

| 行为描述 | 验证方式 | 状态 |
|----------|----------|------|
| **ClickHouse 接入验证** | `invoke clickhouse.status` | ✅ Active |

---

## Used by

- [docs/ssot/README.md](./README.md)
- [platform/11.signoz](../platform/11.signoz/README.md)
