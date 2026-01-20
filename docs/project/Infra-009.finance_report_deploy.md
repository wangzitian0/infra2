# Infra-009: Finance Report Deployment

**Status**: In Progress  
**Owner**: Infra  
**Priority**: P1  
**Created**: 2026-01-10

## Goal

Deploy Finance Report application (FastAPI + Next.js) with independent PostgreSQL and Redis instances.

## Context

Finance Report is a personal financial management system that requires:
- PostgreSQL 16 for double-entry bookkeeping data
- Redis for caching/sessions
- MinIO for statement file storage (shared with platform)

The deployment uses vault-init pattern for secrets management.

## Scope

This project is managed in the **finance_report** repository. See the main EPIC document:

👉 **[EPIC-007.deployment.md](https://github.com/wangzitian0/finance_report/blob/main/docs/project/EPIC-007.deployment.md)**

## Architecture

```
finance_report/finance_report/
├── 01.postgres/     # Dedicated PostgreSQL instance
├── 02.redis/        # Dedicated Redis instance
└── 10.app/          # Backend + Frontend application
```

## Dependencies

```
vault → postgres → app
        redis ──────┘
        minio (platform/03.minio)
```

## Domain

- **Production**: `report.${INTERNAL_DOMAIN}`
- **Single domain for FE + BE** (API at `/api/*`)

## Deliverables

- finance_report/finance_report/01.postgres/
- finance_report/finance_report/02.redis/
- finance_report/finance_report/10.app/

## Vault Secrets Setup

Before deploying the application, configure the following secrets in Vault:

### App Secrets (secret/data/finance_report/production/app)

```bash
# 1. Database connection string
invoke env.set DATABASE_URL=postgresql+asyncpg://postgres:<PASSWORD>@finance_report-postgres:5432/finance_report \
  --project=finance_report --env=production --service=app

# 2. Redis connection string
invoke env.set REDIS_URL=redis://:<PASSWORD>@finance_report-redis:6379/0 \
  --project=finance_report --env=production --service=app

# 3. MinIO/S3 configuration (internal endpoint)
invoke env.set S3_ENDPOINT=http://platform-minio:9000 \
  --project=finance_report --env=production --service=app

# 4. MinIO/S3 public endpoint (REQUIRED for OpenRouter AI access)
invoke env.set S3_PUBLIC_ENDPOINT=https://s3.zitian.party \
  --project=finance_report --env=production --service=app

# 5. MinIO access credentials
invoke env.set S3_ACCESS_KEY=finance-report-app \
  --project=finance_report --env=production --service=app

invoke env.set S3_SECRET_KEY=<GENERATED_SECRET_KEY> \
  --project=finance_report --env=production --service=app

# 6. MinIO bucket name
invoke env.set S3_BUCKET=finance-report-statements \
  --project=finance_report --env=production --service=app

# 7. OpenRouter API key (for AI document parsing)
invoke env.set OPENROUTER_API_KEY=<YOUR_OPENROUTER_API_KEY> \
  --project=finance_report --env=production --service=app
```

### MinIO Security Configuration

MinIO bucket and user setup is **automated** via the `invoke fr-app.setup` command. The deployment script calls the shared `create_app_bucket` task from `platform/03.minio/shared_tasks.py`.

**What happens automatically:**
1. Creates bucket `finance-report-statements`
2. Generates MinIO service account credentials (if not already in Vault)
3. Configures bucket with security best practices:
   - Public download access (required for OpenRouter to access files via direct public URLs)
   - Server-side encryption (SSE-S3)
   - Lifecycle policy: auto-delete files after 90 days
4. Stores credentials in Vault (`secret/data/finance_report/production/app`)

**To manually configure or recreate bucket:**

```bash
# Using the shared MinIO task
invoke minio.create-app-bucket \
  --bucket-name=finance-report-statements \
  --enable-encryption=True \
  --lifecycle-days=90 \
  --public-download=True
```

**Security Impact**:
- ✅ Public can download files anonymously when the object URL is known (public-read bucket)
- ❌ Public **cannot** list bucket contents
- ❌ Public **cannot** upload or delete files
- ✅ Only application service account can upload/delete
- ✅ Data encrypted at rest (SSE-S3)
- ✅ Auto-deletion after 90 days reduces exposure window

**Optional Manual Hardening:**

```bash
# Enable versioning (protect against accidental deletion)
docker exec platform-minio mc version enable local/finance-report-statements

# Enable audit logging (track file access)
# Via MinIO Console: https://minio.zitian.party
# Settings → Audit → Enable
```

**Verification:**

```bash
# Check bucket configuration
docker exec platform-minio mc ls local/finance-report-statements
docker exec platform-minio mc anonymous get local/finance-report-statements  # Should show "download"
docker exec platform-minio mc encrypt info local/finance-report-statements  # Should show "sse-s3"
docker exec platform-minio mc ilm ls local/finance-report-statements  # Should show 90-day expiry
```

### Critical: S3_PUBLIC_ENDPOINT

The `S3_PUBLIC_ENDPOINT` is **required** for production deployments because:
- OpenRouter AI service needs to download statement PDFs for parsing
- Internal endpoints (e.g., `http://platform-minio:9000`) are not accessible from OpenRouter
- Without this, system falls back to Base64 encoding which is limited to files <5MB
- Production value: `https://s3.zitian.party` (publicly accessible MinIO S3 API endpoint)

## Observability (SigNoz Logs)

- OTEL export is **optional** in app code; enabled via Vault in staging/production.
- OTEL endpoint should be environment-specific (no placeholders in Vault).
- Required keys in Vault: `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`.

## PR Links

- infra2: https://github.com/wangzitian0/infra2/pull/67

## Change Log

| Date | Change |
|------|--------|
| 2026-01-10 | Initialized project, linked to EPIC-007 |
| 2026-01-19 | Added SigNoz log export wiring for finance_report app |
| 2026-01-19 | Set OTEL Vault values for finance_report app (staging/production) |
| 2026-01-19 | Added restart-safe compose wiring and fixed Vault template quoting |

## Verification

- [ ] `invoke finance_report.postgres.status`
- [ ] `invoke finance_report.redis.status`
- [ ] `invoke finance_report.app.status`
- [ ] `curl https://report.${INTERNAL_DOMAIN}`
- [ ] Logs visible in SigNoz (after OTEL vars are set)

## References

- [SSOT: platform.domain](../ssot/platform.domain.md)
- [SSOT: bootstrap.vars_and_secrets](../ssot/bootstrap.vars_and_secrets.md)
- [Platform README](../../platform/README.md)
