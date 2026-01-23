# Prefect

> **Purpose**: Workflow orchestration platform for automating data pipelines and scheduled tasks.

## Overview

Prefect is a modern workflow orchestration platform that allows building, scheduling, and monitoring data pipelines. This deployment includes:

- **prefect-server**: API server and web UI
- **prefect-services**: Background services (scheduler, flow runner)
- **prefect-worker**: Worker for executing flows in the default pool
- **postgres**: Stores flow runs, deployments, and task state (shared platform DB)
- **redis**: Message broker for event streaming (shared platform Redis, DB 1)

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Traefik                                │
│     prefect.{domain} → Authentik ForwardAuth → Prefect UI  │
└─────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
  ┌─────▼─────┐      ┌────────▼────────┐     ┌─────▼──────┐
  │ Postgres  │      │  Platform Redis │     │   Worker   │
  │ (shared)  │      │  (shared, DB 1) │     │            │
  └───────────┘      └─────────────────┘     └────────────┘
```

## Dependencies

| Service | Purpose |
|---------|---------|
| postgres | Flow run and deployment storage (shared platform-postgres) |
| redis | Message broker and cache (shared platform-redis, DB 1) |
| vault | Secrets management |
| authentik | SSO authentication |

## Quick Start

```bash
# 1. Deploy (requires postgres and vault to be up)
invoke prefect.setup

# 2. Configure SSO protection
invoke authentik.shared.create-proxy-app \
  --name=Prefect \
  --slug=prefect \
  --external-host=https://prefect.{domain} \
  --internal-host=platform-prefect-server \
  --port=4200

# 3. Check status
invoke prefect.status
```

## Configuration

### Secrets in Vault

Path: `secret/platform/{env}/postgres` (reuses shared postgres password)

### Environment Variables

| Variable | Value | Description |
|----------|-------|-------------|
| `PREFECT_API_DATABASE_CONNECTION_URL` | `postgresql+asyncpg://postgres:***@platform-postgres:5432/prefect` | Database connection |
| `PREFECT_MESSAGING_BROKER` | `prefect_redis.messaging` | Redis messaging backend |
| `PREFECT_REDIS_MESSAGING_HOST` | `platform-redis` | Shared platform Redis |
| `PREFECT_REDIS_MESSAGING_DB` | `1` | Redis DB (0=platform cache, 1=prefect) |

### Shared Redis Usage

Prefect uses **platform-redis DB 1** to avoid conflicts:
- **DB 0**: Platform cache (shared by other services)
- **DB 1**: Prefect messaging (real-time events, flow coordination)

## Access

- **URL**: `https://prefect{ENV_DOMAIN_SUFFIX}.{INTERNAL_DOMAIN}`
- **Auth**: Authentik SSO (admins group)

## Usage

### Creating Flows

From any machine with Prefect installed:

```bash
export PREFECT_API_URL=https://prefect.{domain}/api
prefect work-pool create my-pool --type process
prefect deploy my_flow.py:my_flow --pool my-pool
```

### Monitoring

- **UI**: `https://prefect.{domain}` - Flow runs, logs, metrics
- **CLI**: `prefect flow-run ls` - List recent runs

## Troubleshooting

### Health Check Failed

```bash
# Check server logs
ssh root@{VPS_HOST} docker logs platform-prefect-server

# Check secrets are rendered
ssh root@{VPS_HOST} docker exec platform-prefect-vault-agent cat /vault/secrets/.env
```

### Database Connection Issues

```bash
# Verify postgres is running
invoke postgres.status

# Check database exists
ssh root@{VPS_HOST} docker exec platform-postgres psql -U postgres -c '\l' | grep prefect
```

### Worker Not Picking Up Flows

```bash
# Check worker status
ssh root@{VPS_HOST} docker logs platform-prefect-worker

# Verify work pool exists
curl https://prefect.{domain}/api/work_pools/
```

## References

- [Prefect Documentation](https://docs.prefect.io/v3/)
- [Self-hosted Guide](https://docs.prefect.io/v3/how-to-guides/self-hosted/docker-compose)
- [Platform README](../README.md)
- [Vault Integration SSOT](../../docs/ssot/db.vault-integration.md)
- [SSO SSOT](../../docs/ssot/platform.sso.md)
