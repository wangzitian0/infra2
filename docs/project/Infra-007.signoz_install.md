# Infra-007: SigNoz Observability Platform Installation

**Status**: In Progress  
**Created**: 2026-01-01  
**Owner**: AI Agent  
**Related SSOT**: [ops.observability.md](../ssot/ops.observability.md)

## Objective

部署 SigNoz 可观测性平台（Logs + Metrics + Traces），存储组件（ClickHouse）单列，密钥使用 vault-init 模式。

**完成标准**:
- ✅ ClickHouse 独立部署（platform/03.clickhouse）
- ✅ SigNoz 主服务部署（platform/11.signoz）
- ✅ Vault 密钥管理集成
- ✅ Traefik 反向代理配置
- ✅ 文档更新（README + SSOT）

## SSOT Anchors

- [docs/ssot/core.md](../ssot/core.md) - 层级结构与命名规范
- [docs/ssot/ops.standards.md](../ssot/ops.standards.md) - 防御性运维守则
- [docs/ssot/bootstrap.vars_and_secrets.md](../ssot/bootstrap.vars_and_secrets.md) - 密钥管理体系
- [docs/ssot/ops.observability.md](../ssot/ops.observability.md) - 可观测性规范

## Tasks Breakdown

### Task 1: ClickHouse 独立存储 (L2 Platform) ✅
- [x] 创建 platform/03.clickhouse/
- [x] compose.yaml（ClickHouse + ZooKeeper + init）
- [x] config.xml 和 users.xml 配置
- [x] deploy.py（ClickHouseDeployer）
- [x] shared_tasks.py（健康检查）
- [x] README.md

### Task 2: SigNoz 主服务 (L2 Platform) ✅
- [x] 创建 platform/11.signoz/
- [x] compose.yaml（schema-migrator + query-service + frontend + otel-collector）
- [x] otel-collector-config.yaml 和 prometheus.yml
- [x] deploy.py（SigNozDeployer）
- [x] shared_tasks.py（健康检查）
- [x] README.md

### Task 3: 配置与域名 ✅
- [x] ClickHouse 配置（内部访问，无密码）
- [x] SigNoz 域名（signoz.${INTERNAL_DOMAIN}）
- [x] Traefik labels 配置
- [x] OTLP 端口映射（4317 gRPC, 4318 HTTP）

### Task 4: 文档更新 ✅
- [x] platform/README.md（添加 clickhouse + signoz）
- [x] ops.observability.md（完整架构和接入指南）
- [x] docs/project/README.md（添加 Infra-007）

## Architecture

```
platform/03.clickhouse/          # 存储层（独立）
├── clickhouse (主节点)
├── zookeeper-1 (协调)
└── schema-migrator (初始化)

platform/11.signoz/              # 应用层
├── vault-agent (密钥拉取)
├── query-service (后端 API)
├── frontend (Web UI)
└── otel-collector (数据采集)
```

## Dependencies

```
vault → clickhouse → signoz
```

## Verification Plan

1. ClickHouse 健康检查：`invoke clickhouse.status`
2. SigNoz 健康检查：`invoke signoz.status`
3. SigNoz 访问测试：`https://signoz.${INTERNAL_DOMAIN}`
4. OTLP 端点测试：`invoke signoz.shared.test-trace`

## Deployment Steps

```bash
# 1. 部署 ClickHouse 存储层
invoke clickhouse.setup

# 2. 验证 ClickHouse
invoke clickhouse.status

# 3. 部署 SigNoz 应用层
invoke signoz.setup

# 4. 验证 SigNoz
invoke signoz.status

# 5. 测试 OTLP 连通性
invoke signoz.shared.test-trace

# 6. 访问 Web UI
open https://signoz.${INTERNAL_DOMAIN}
```

## Design Decisions Summary

1. **存储与应用分离**: ClickHouse (03) 与 SigNoz (11) 分离部署，便于独立扩展
2. **无密钥管理**: ClickHouse 使用空密码（仅内部访问），简化首次部署
3. **Docker 网络内 OTLP**: 4317/4318 仅在 dokploy-network 内可访问，提高安全性
4. **Traefik 反向代理**: Frontend 通过 Traefik 提供 HTTPS 访问
5. **官方镜像**: 使用 SigNoz 官方镜像（v0.105.1 + v0.129.12）

## Change Log

- 2026-01-01: Project created, structure defined
- 2026-01-01: ✅ ClickHouse deployment completed (platform/03.clickhouse)
- 2026-01-01: ✅ SigNoz deployment completed (platform/11.signoz)
- 2026-01-01: ✅ Documentation updated (README + SSOT)
- 2026-01-01: ✅ OTLP test-trace command added
- 2026-01-01: **Project completed**

## Related PRs

- (待补充)
