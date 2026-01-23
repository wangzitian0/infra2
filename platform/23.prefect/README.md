# Perfect (Prefect Workflow Orchestration)

> **Purpose**: Workflow orchestration platform for automating data pipelines and scheduled tasks.
> **Deployment**: Single global instance (production only), environment isolation via Prefect projects.

## Overview

Perfect is a modern workflow orchestration platform powered by Prefect. This deployment includes:

- **prefect-server**: API server and web UI (SSO-protected at https://perfect.zitian.party)
- **prefect-services**: Background services (scheduler, flow runner)
- **prefect-worker**: Worker for executing flows in the default pool
- **postgres**: Stores flow runs, deployments, and task state (shared platform DB)
- **redis**: Message broker for event streaming (shared platform Redis, DB 1)

### Environment Strategy

**Single global deployment** - No per-environment instances (staging/production).
Use **Prefect projects** and **tags** for environment isolation:
- Project: `finance-report-prod`, `finance-report-staging`
- Tags: `env:production`, `env:staging`

## Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                         Traefik                                │
│  perfect.zitian.party → Authentik ForwardAuth → Prefect UI    │
│  (Browser access only - requires SSO login)                    │
└────────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                      │
  ┌─────▼─────┐      ┌────────▼────────┐     ┌──────▼──────┐
  │ Postgres  │      │  Platform Redis │     │   Worker    │
  │ (shared)  │      │  (shared, DB 1) │     │             │
  └───────────┘      └─────────────────┘     └─────────────┘
                              ▲
                              │
               ┌──────────────┴──────────────┐
               │ Internal API (no SSO)       │
               │ http://platform-prefect-    │
               │ server:4200/api             │
               │ (Docker network access)     │
               └─────────────────────────────┘
```

**Key Points**:
- **UI Access**: `https://perfect.zitian.party` (SSO required)
- **API Access**: `http://platform-prefect-server:4200/api` (no auth, Docker network only)
- Python SDK/CLI use internal API, not affected by SSO

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

# 2. Configure SSO (manual via Web UI)
# Visit https://sso.zitian.party
# Create Provider: Proxy, external=https://perfect.zitian.party, internal=http://platform-prefect-server:4200
# Create Application: name=Perfect, slug=perfect

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

- **Web UI**: `https://perfect.zitian.party` (SSO-protected, browser only)
- **API Endpoint** (for Python SDK/CLI): `http://platform-prefect-server:4200/api` (internal, no auth)

## Usage

### From Your Python Application (Internal API)

```python
import os
from prefect import flow

# Use internal API endpoint (no SSO required)
os.environ['PREFECT_API_URL'] = 'http://platform-prefect-server:4200/api'

@flow(name="my-workflow")
def my_workflow():
    # Your workflow logic
    pass

if __name__ == "__main__":
    my_workflow()
```

### Environment Isolation

Use **Prefect projects** to separate environments:

```bash
# Deploy to production project
prefect project create finance-report-prod
prefect --project finance-report-prod deploy my_flow.py:my_flow --tags env:production

# Deploy to staging project  
prefect project create finance-report-staging
prefect --project finance-report-staging deploy my_flow.py:my_flow --tags env:staging
```

### Work Pools

```bash
prefect work-pool create production-pool --type process
prefect work-pool create staging-pool --type process
```

### Monitoring

- **Web UI**: `https://perfect.zitian.party` - View flow runs, logs, metrics (requires SSO login)
- **CLI**: Configure with internal API to list runs programmatically

```bash
export PREFECT_API_URL=http://platform-prefect-server:4200/api
prefect flow-run ls
```

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

# Check API connectivity from worker
ssh root@{VPS_HOST} docker exec platform-prefect-worker curl http://platform-prefect-server:4200/api/health
```

### Cannot Access UI (404 or Auth Loop)

```bash
# Verify SSO app exists in Authentik
# Visit https://sso.zitian.party/if/admin/#/core/applications
# Should see "Perfect" application with slug "perfect"

# Check Traefik routing
ssh root@{VPS_HOST} docker logs traefik 2>&1 | grep perfect
```

## References

- [Prefect Documentation](https://docs.prefect.io/v3/)
- [Self-hosted Guide](https://docs.prefect.io/v3/how-to-guides/self-hosted/docker-compose)
- [Platform README](../README.md)
- [Vault Integration SSOT](../../docs/ssot/db.vault-integration.md)
- [SSO SSOT](../../docs/ssot/platform.sso.md)
