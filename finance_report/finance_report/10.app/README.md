# Finance Report Application

> **Purpose**: Backend (FastAPI) + Frontend (Next.js) for Finance Report.

## Overview

- **Domain**: `report${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}`
- **Backend Container**: `finance_report-backend${ENV_SUFFIX}` (port 8000)
- **Frontend Container**: `finance_report-frontend${ENV_SUFFIX}` (port 3000)

## Routing

Single domain with path-based routing:

| Path | Target | Notes |
|------|--------|-------|
| `/api/*` | Backend | Strips `/api` prefix |
| `/*` | Frontend | Catch-all |

## Secrets (Vault)

Secrets are read from multiple Vault paths:

### Dynamic Construction (via secrets.ctmpl)

`DATABASE_URL` and `REDIS_URL` are constructed dynamically using ENV_SUFFIX pattern:

- Reads `POSTGRES_PASSWORD` from `secret/data/finance_report/<env>/postgres`
- Reads redis `PASSWORD` from `secret/data/finance_report/<env>/redis`
- Constructs URLs with environment-specific hostnames (e.g., `-staging` suffix)

### Application Secrets

Stored at: `secret/data/finance_report/<env>/app`

| Key | Description |
|-----|-------------|
| `S3_ENDPOINT` | MinIO/S3 endpoint URL |
| `S3_ACCESS_KEY` | MinIO/S3 access key |
| `S3_SECRET_KEY` | MinIO/S3 secret key |
| `S3_BUCKET` | Bucket name for statements |
| `OPENROUTER_API_KEY` | OpenRouter API key for Gemini |
| `NEXT_PUBLIC_APP_URL` | Frontend URL used by backend-generated links |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | SigNoz OTLP HTTP endpoint |
| `OTEL_SERVICE_NAME` | OTEL service name for logs |
| `OTEL_RESOURCE_ATTRIBUTES` | OTEL resource attributes (e.g., environment) |

## Quick Start

```bash
# Deploy (after postgres and redis are ready)
invoke finance_report.app.setup

# Check status
invoke finance_report.app.status

# Access
open https://report.${INTERNAL_DOMAIN}
```

## Build Configuration

For the standard deployment using pre-built images from `ghcr.io` (via `invoke finance_report.app.setup`), no additional build-time configuration is required. The images are built and pushed by GitHub Actions.

Optional environment variables:
- `IMAGE_TAG`: Docker image tag (default: `latest`)

For local development builds, see the [finance_report repository](https://github.com/wangzitian0/finance_report).

## Health Checks

- Backend: `GET /health` on port 8000
- Frontend: `GET /` on port 3000

## Connection Strings

Connection strings are constructed dynamically by vault-agent using the ENV_SUFFIX pattern:

```bash
# Production (ENV_SUFFIX="")
DATABASE_URL=postgresql+asyncpg://postgres:<password>@finance_report-postgres:5432/finance_report
REDIS_URL=redis://:<password>@finance_report-redis:6379/0

# Staging (ENV_SUFFIX="-staging")
DATABASE_URL=postgresql+asyncpg://postgres:<password>@finance_report-postgres-staging:5432/finance_report
REDIS_URL=redis://:<password>@finance_report-redis-staging:6379/0

# PR Environment (ENV_SUFFIX="-pr-123")
DATABASE_URL=postgresql+asyncpg://postgres:<password>@finance_report-postgres-pr-123:5432/finance_report
REDIS_URL=redis://:<password>@finance_report-redis-pr-123:6379/0
```

This ensures correct hostname resolution in Dokploy's shared network where each environment has isolated containers.

## References

- [PostgreSQL](../01.postgres/README.md)
- [Redis](../02.redis/README.md)
- [Platform MinIO](../../../platform/03.minio/README.md)
- [Vault Integration](../../../docs/ssot/db.vault-integration.md)
