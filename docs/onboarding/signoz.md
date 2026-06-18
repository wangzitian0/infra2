# SigNoz Deployment Guide

> **Quick reference for deploying SigNoz observability platform**

## Overview

SigNoz is deployed in two parts:
1. **Storage Layer** (platform/03.clickhouse): ClickHouse + ZooKeeper
2. **Application Layer** (platform/11.signoz): Query Service + Frontend + OTLP Collector

## Prerequisites

- Vault must be accessible
- DNS configured for `signoz${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}`
- Ports `${OTEL_GRPC_PORT}`, `${OTEL_HTTP_PORT}` available for OTLP (staging defaults: 14317/14318)

> `ENV_SUFFIX` 生产为空；staging 请在 Dokploy Environment 显式设为 `-staging`（避免容器冲突）。

## Quick Deploy

```bash
# 1. Deploy ClickHouse storage
python -m tools.deploy_v2 --service platform/clickhouse --type prod --iac-ref vX.Y.Z --domain zitian.party --code-reviewed

# 2. Verify ClickHouse health
invoke clickhouse.status

# 3. Deploy SigNoz services
python -m tools.deploy_v2 --service platform/signoz --type prod --iac-ref vX.Y.Z --domain zitian.party --code-reviewed

# 4. Verify SigNoz health
invoke signoz.status
```

## Access

- **Web UI**: `https://signoz${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}`
- **OTLP gRPC**: `platform-signoz-otel-collector${ENV_SUFFIX}:4317` (Docker network only)
- **OTLP HTTP**: `platform-signoz-otel-collector${ENV_SUFFIX}:4318` (Docker network only)

> **Note**: OTLP endpoints are only accessible within the `dokploy-network`. 
> Use `invoke signoz.shared.test-trace` to verify connectivity.

## File Structure

```
platform/
├── 03.clickhouse/              # Storage layer
│   ├── compose.yaml            # ClickHouse + ZooKeeper
│   ├── config.xml              # Server config
│   ├── users.xml               # Auth config
│   ├── deploy.py               # Deployer
│   └── README.md
│
├── 11.signoz/                  # Application layer
│   ├── compose.yaml            # SigNoz services
│   ├── otel-collector-config.yaml
│   ├── prometheus.yml
│   ├── deploy.py               # Deployer
│   └── README.md
│
└── signoz-official-compose.yaml # Reference
```

## Architecture

```
┌─────────────┐
│ Apps (OTLP) │
└──────┬──────┘
       │
       v
┌──────────────────┐     ┌───────────────┐
│ OTLP Collector   │────>│ ClickHouse    │
│ :4317/:4318      │     │ + ZooKeeper   │
└──────────────────┘     └───────┬───────┘
                                 │
                                 v
┌──────────────────┐     ┌───────────────┐
│ Frontend         │<────│ Query Service │
│ signoz${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN} │     │               │
└──────────────────┘     └───────────────┘
```

## Troubleshooting

### ClickHouse not starting
```bash
# Check logs
docker logs platform-clickhouse${ENV_SUFFIX}

# Re-initialize directories
invoke clickhouse.pre-compose
```

### SigNoz 502 error
```bash
# Check signoz health
docker logs platform-signoz${ENV_SUFFIX}

# Ensure ClickHouse is ready
invoke clickhouse.status
```

### OTLP data not appearing
```bash
# Check collector logs
docker logs platform-signoz-otel-collector${ENV_SUFFIX}

# Send a test trace
invoke signoz.shared.test-trace

# Verify ClickHouse connection
docker exec platform-clickhouse${ENV_SUFFIX} clickhouse-client --query "SHOW DATABASES"
```

## Next Steps

1. Configure first application to send OTLP data
2. Create dashboards in SigNoz UI
3. Set up alerts (optional)
4. Consider SSO integration (future)

## Documentation

- Project: [docs/project/Infra-007.signoz_install.md](docs/project/Infra-007.signoz_install.md)
- SSOT: [docs/ssot/ops.observability.md](docs/ssot/ops.observability.md)
- ClickHouse: [platform/03.clickhouse/README.md](platform/03.clickhouse/README.md)
- SigNoz: [platform/11.signoz/README.md](platform/11.signoz/README.md)
