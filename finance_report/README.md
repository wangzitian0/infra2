# Finance Report Deployment

> **Purpose**: Deploy Finance Report application with dedicated database and cache instances.
> **Boundary**: This directory contains only the IaC orchestration coordinates, Dokploy Compose specifications, and deployment tasks for Finance Report. The application source code, business logic, domain models, and developer guides reside in the autonomous repository `repos/finance_report`.

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
| app | postgres, redis, s3 | S3 (RustFS) from platform/03.s3 |

## Quick Start

```bash
# 1. Deploy database layer through deploy_v2
python -m tools.deploy_v2 --service finance_report/postgres --type staging --iac-ref vX.Y.Z --domain zitian.party
python -m tools.deploy_v2 --service finance_report/redis --type staging --iac-ref vX.Y.Z --domain zitian.party

# 2. Deploy application through deploy_v2
python -m tools.deploy_v2 --service finance_report/app --type staging --version-ref vX.Y.Z --iac-ref vX.Y.Z --domain zitian.party

# 3. Verify
invoke fr-postgres.shared.status
invoke fr-redis.shared.status
invoke fr-app.shared.status

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
  - LLM_API_KEY
  - LLM_ENCRYPTION_KEYS
  - S3_ACCESS_KEY
  - S3_SECRET_KEY
  - S3_PUBLIC_ACCESS_KEY
  - S3_PUBLIC_SECRET_KEY
  - SECRET_KEY
```

The template composes `DATABASE_URL` and `REDIS_URL` from the `postgres` and `redis` paths. The other app variables are not in Vault: `compose.yaml` or the application default sets each one, and `env.manifest.json` names its source. `10.app/secrets.ctmpl` is the one source for this list.

Model keys are not part of this list. The deploy does not render them (tests in `common/tests` assert it). The infra2 SDK defaults apply.

## References

- **Source Code**: [github.com/wangzitian0/finance_report](https://github.com/wangzitian0/finance_report)
- **EPIC-007**: [docs/project/EPIC-007.deployment.md](https://github.com/wangzitian0/finance_report/blob/main/docs/project/EPIC-007.deployment.md)
- **SSOT**: [docs/ssot/platform.domain.md](../docs/ssot/platform.domain.md)
- **Platform README**: [platform/README.md](../platform/README.md)
