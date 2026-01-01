# SigNoz Deployment Guide

> **Quick reference for deploying SigNoz observability platform**

## Overview

SigNoz is deployed in two parts:
1. **Storage Layer** (platform/03.clickhouse): ClickHouse + ZooKeeper
2. **Application Layer** (platform/11.signoz): Query Service + Frontend + OTLP Collector

## Prerequisites

- Vault must be accessible
- DNS configured for `signoz.${INTERNAL_DOMAIN}`
- Ports 4317, 4318 available for OTLP

## Quick Deploy

```bash
# 1. Deploy ClickHouse storage
invoke clickhouse.setup

# 2. Verify ClickHouse health
invoke clickhouse.status

# 3. Deploy SigNoz services
invoke signoz.setup

# 4. Verify SigNoz health
invoke signoz.status
```

## Access

- **Web UI**: `https://signoz.${INTERNAL_DOMAIN}`
- **OTLP gRPC**: `platform-signoz-otel-collector:4317` (Docker network only)
- **OTLP HTTP**: `platform-signoz-otel-collector:4318` (Docker network only)

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
│ signoz.domain    │     │               │
└──────────────────┘     └───────────────┘
```

## Troubleshooting

### ClickHouse not starting
```bash
# Check logs
docker logs platform-clickhouse

# Re-initialize directories
invoke clickhouse.pre-compose
```

### SigNoz 502 error
```bash
# Check signoz health
docker logs platform-signoz

# Ensure ClickHouse is ready
invoke clickhouse.status
```

### OTLP data not appearing
```bash
# Check collector logs
docker logs platform-signoz-otel-collector

# Send a test trace
invoke signoz.test-trace

# Verify ClickHouse connection
docker exec platform-clickhouse clickhouse-client --query "SHOW DATABASES"
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
