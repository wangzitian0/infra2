# Finance Report Deployment

> **Purpose**: Deploy Finance Report application with dedicated database and cache instances.

## Architecture

```
finance_report/
├── finance_report/           # Application layer
│   ├── 01.postgres/         # Dedicated PostgreSQL 16
│   ├── 02.redis/            # Dedicated Redis
│   └── 10.app/              # Backend (FastAPI) + Frontend (Next.js)
└── README.md                # This file
```

## Domain

- **Production**: `report.${INTERNAL_DOMAIN}`
- Single domain serves both frontend and API (backend at `/api/*`)

## Dependencies

| Service | Depends On | Notes |
|---------|------------|-------|
| postgres | vault | Secrets from Vault |
| redis | vault | Secrets from Vault |
| app | postgres, redis, minio | MinIO from platform/03.minio |

## Quick Start

```bash
# 1. Deploy database layer
invoke finance_report.postgres.setup
invoke finance_report.redis.setup

# 2. Deploy application
invoke finance_report.app.setup

# 3. Verify
invoke finance_report.postgres.status
invoke finance_report.redis.status
invoke finance_report.app.status

# 4. Access
open https://report.${INTERNAL_DOMAIN}
```

## Vault Secrets

Secrets are stored in Vault KV v2:

```
secret/data/finance_report/<env>/postgres
  - POSTGRES_PASSWORD

secret/data/finance_report/<env>/redis
  - PASSWORD

secret/data/finance_report/<env>/app
  - DATABASE_URL
  - REDIS_URL
  - S3_ENDPOINT
  - S3_ACCESS_KEY
  - S3_SECRET_KEY
  - S3_BUCKET
  - OPENROUTER_API_KEY
```

## References

- **Source Code**: [github.com/wangzitian0/finance_report](https://github.com/wangzitian0/finance_report)
- **EPIC-007**: [docs/project/EPIC-007.deployment.md](https://github.com/wangzitian0/finance_report/blob/main/docs/project/EPIC-007.deployment.md)
- **SSOT**: [docs/ssot/platform.domain.md](../docs/ssot/platform.domain.md)
- **Platform README**: [platform/README.md](../platform/README.md)
