# ClickHouse SSOT

> **SSOT Key**: `db.clickhouse`
> **核心定义**: ClickHouse (SigNoz Storage).

---

## 1. 真理来源 (The Source)

| 维度 | 物理位置 (SSOT) | 说明 |
|------|----------------|------|
| **实例定义** | [`platform/03.clickhouse/compose.yaml`](../../platform/03.clickhouse/compose.yaml) | ClickHouse + ZooKeeper 服务配置 |
| **部署任务** | [`platform/03.clickhouse/deploy.py`](../../platform/03.clickhouse/deploy.py) | `ClickHouseDeployer` |
| **服务端配置** | [`platform/03.clickhouse/config.xml`](../../platform/03.clickhouse/config.xml) | 集群与内存上限模板 |

- **用途**: SigNoz 可观测性平台的存储后端 (Logs, Metrics, Traces)。
- **架构**: 单节点 ClickHouse + ZooKeeper (协调)，仅供 SigNoz 内网使用。

---

## 2. 验证与测试 (The Proof)

| 行为描述 | 验证方式 | 状态 |
|----------|----------|------|
| **ClickHouse 写路径健康检查** | `DEPLOY_ENV=<env> invoke clickhouse.status` | ✅ Active |
| **ClickHouse 配置与内存上限校验** | `common/tests/test_clickhouse_config.py`, `common/tests/test_clickhouse_memory_caps.py` | ✅ Automated |

---

## Used by

- [docs/ssot/README.md](./README.md)
- [platform/11.signoz](../../platform/11.signoz/README.md)
