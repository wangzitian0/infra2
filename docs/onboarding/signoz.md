# SigNoz Deployment Guide

> **Quick reference for deploying SigNoz observability platform**
> **权威定义**: 可观测性架构、OTLP 接入、告警规则与时间尺度分层由 [`docs/ssot/ops.observability.md`](../ssot/ops.observability.md) 单一拥有；ClickHouse 存储层契约见 [`docs/ssot/db.clickhouse.md`](../ssot/db.clickhouse.md)。

## Overview

SigNoz is deployed in two parts:
1. **Storage Layer** ([`platform/03.clickhouse`](../../platform/03.clickhouse/README.md)): ClickHouse + ZooKeeper
2. **Application Layer** ([`platform/11.signoz`](../../platform/11.signoz/README.md)): Query Service + Frontend + OTLP Collector

## Prerequisites

- Vault must be accessible
- DNS configured for `signoz${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}`
- Ports `${OTEL_GRPC_PORT}`, `${OTEL_HTTP_PORT}` available for OTLP (staging defaults: 14317/14318)

> 环境隔离与 `ENV_SUFFIX` 规范见 [`docs/ssot/core.environments.md`](../ssot/core.environments.md)。

## Quick Deploy

```bash
# 1. Deploy ClickHouse storage
python -m tools.deploy_v2 --service platform/clickhouse --type prod --iac-ref vX.Y.Z --domain zitian.party --code-reviewed

# 2. Verify ClickHouse health
DEPLOY_ENV=production invoke clickhouse.status

# 3. Deploy SigNoz services
python -m tools.deploy_v2 --service platform/signoz --type prod --iac-ref vX.Y.Z --domain zitian.party --code-reviewed

# 4. Verify SigNoz health
DEPLOY_ENV=production invoke signoz.status
```

## Access

- **Web UI**: `https://signoz${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}`
- **OTLP Public Ingest (Browser FE)**: `https://otel${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}` (CORS allowlisted)
- **OTLP Internal (Docker network)**: `platform-signoz-otel-collector${ENV_SUFFIX}:4317` (gRPC) / `:4318` (HTTP)

> Use `DEPLOY_ENV=<env> invoke signoz.shared.test-trace` to verify connectivity.

## Troubleshooting

### ClickHouse not starting
```bash
# Check logs
docker logs platform-clickhouse${ENV_SUFFIX}

# Re-initialize directories
DEPLOY_ENV=<env> invoke clickhouse.pre-compose
```

### SigNoz 502 error
```bash
# Check signoz health
docker logs platform-signoz${ENV_SUFFIX}

# Ensure ClickHouse is ready
DEPLOY_ENV=<env> invoke clickhouse.status
```

### OTLP data not appearing
```bash
# Check collector logs
docker logs platform-signoz-otel-collector${ENV_SUFFIX}

# Send a test trace
DEPLOY_ENV=<env> invoke signoz.shared.test-trace

# Verify ClickHouse connection
docker exec platform-clickhouse${ENV_SUFFIX} clickhouse-client --query "SHOW DATABASES"
```

## Next Steps

1. Configure first application to send OTLP data
2. Create dashboards in SigNoz UI
3. Set up alerts (optional)
4. Consider SSO integration (future)

## Documentation

- Project: [docs/project/Infra-007.signoz_install.md](../project/archive/Infra-007.signoz_install.md)
- SSOT: [docs/ssot/ops.observability.md](../ssot/ops.observability.md)
- ClickHouse: [platform/03.clickhouse/README.md](../../platform/03.clickhouse/README.md)
- SigNoz: [platform/11.signoz/README.md](../../platform/11.signoz/README.md)
