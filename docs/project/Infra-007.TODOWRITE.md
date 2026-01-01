# Infra-007 TODOWRITE

**Project**: SigNoz Observability Platform Installation  
**Status**: In Progress

## Artifacts & Notes

### Official Compose Analysis

**Downloaded**: `platform/signoz-official-compose.yaml`

**Core Components**:
- ClickHouse: 存储后端（时序数据库）
- ZooKeeper: ClickHouse 集群协调
- query-service: 后端 API 服务
- frontend: Web UI (端口 3301)
- otel-collector: OpenTelemetry Collector (4317 gRPC, 4318 HTTP)
- alertmanager: 告警管理器

**Key Observations**:
1. ClickHouse 依赖 ZooKeeper（高可用模式）
2. 所有服务依赖 ClickHouse（通过 schema-migrator）
3. 默认端口：frontend 3301, OTLP 4317/4318
4. 使用自定义网络 `signoz-net`

### Design Decisions

1. **存储组件独立**：
   - ClickHouse + ZooKeeper + init → `platform/03.clickhouse`
   - 原因：ClickHouse 是有状态存储，应与应用层分离

2. **应用层**：
   - query-service + frontend + otel-collector → `platform/11.signoz`
   - 原因：这些是无状态应用层，可共同部署

3. **密钥管理**：
   - ClickHouse admin password → Vault
   - SigNoz 暂无需额外密钥（使用 ClickHouse 连接）

4. **域名策略**：
   - Frontend: `signoz.${INTERNAL_DOMAIN}` (Traefik)
   - OTLP 端点: 通过 Dokploy 端口映射（4317, 4318）

### Implementation Notes

#### ClickHouse Volume Paths
```
/data/platform/clickhouse/
├── data/          # ClickHouse 数据
├── logs/          # 日志
└── zookeeper/     # ZooKeeper 数据
```

#### SigNoz Volume Paths
```
/data/platform/signoz/
├── signoz/        # query-service 数据
└── alertmanager/  # alertmanager 配置
```

### TODO Items

- [x] 简化 ClickHouse compose（去除非必要组件）
- [ ] 添加 vault-agent 到 query-service（使用 deploy.py 生成 JWT secret 替代）
- [x] 配置 Traefik labels for frontend
- [x] 测试 OTLP 端点连通性（`invoke signoz.shared.test-trace`）
- [ ] 考虑是否需要 SSO 保护
- [ ] ClickHouse 添加密码认证（安全改进）

### Questions & Decisions

**Q**: 是否需要 SSO 保护？  
**A**: 暂不需要，SigNoz 自带用户管理，可后续集成

**Q**: 是否部署 alertmanager？  
**A**: 第一阶段暂不部署，聚焦核心观测功能

**Q**: ClickHouse 是否需要多节点？  
**A**: 第一阶段单节点 + ZooKeeper，后续可扩展

**Q**: JWT secret 如何管理？
**A**: 通过 Vault 存储，`deploy.py` 自动生成并传递给 Dokploy

---
*Last updated: 2026-01-01*
