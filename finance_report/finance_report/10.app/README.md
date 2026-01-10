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

Secrets stored at: `secret/data/finance_report/<env>/app`

| Key | Description |
|-----|-------------|
| `DATABASE_URL` | PostgreSQL connection string |
| `REDIS_URL` | Redis connection string |
| `S3_ENDPOINT` | MinIO/S3 endpoint URL |
| `S3_ACCESS_KEY` | MinIO/S3 access key |
| `S3_SECRET_KEY` | MinIO/S3 secret key |
| `S3_BUCKET` | Bucket name for statements |
| `OPENROUTER_API_KEY` | OpenRouter API key for Gemini |

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

Build the connection strings from other services:

```bash
# PostgreSQL
DATABASE_URL=postgresql://postgres:<pg_password>@finance_report-postgres${ENV_SUFFIX}:5432/finance_report

# Redis
REDIS_URL=redis://:<redis_password>@finance_report-redis${ENV_SUFFIX}:6379/0

# MinIO (from platform)
S3_ENDPOINT=http://platform-minio${ENV_SUFFIX}:9000
```

## References

- [PostgreSQL](../01.postgres/README.md)
- [Redis](../02.redis/README.md)
- [Platform MinIO](../../../platform/03.minio/README.md)
- [Vault Integration](../../../docs/ssot/db.vault-integration.md)
