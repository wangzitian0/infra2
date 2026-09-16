# Infra-009: Finance Report Deployment
**Status**: Archived — Completed (superseded by deploy_v2 Infra-015)
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

## Change Log

| Date | Change |
|------|--------|
| 2026-08-18 | Aligned the fixed app backend's healthcheck start period with preview's 450-second cold-start budget after a measured 177-second staging boot. |
| 2026-08-18 | Made the finance_report app rate-limit fallback environment-aware: staging 2000, production 300, with Vault override precedence. |
| 2026-06-02 | Scoped finance_report Vault app-token policies by environment and added targeted token repair with accessor tracking/revoke. |

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

MinIO bucket and user setup is automated by the finance_report app bootstrap/repair path. The current staging/prod deploy front door is `deploy_v2`; bucket creation reuses the shared `create_app_bucket` task from `platform/03.minio/shared_tasks.py` when bootstrap/repair needs to materialize missing storage.

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
- infra2: https://github.com/wangzitian0/infra2/pull/116 (S3 endpoint fix)

## Change Log

| Date | Change |
|------|--------|
| 2026-01-10 | Initialized project, linked to EPIC-007 |
| 2026-01-19 | Added SigNoz log export wiring for finance_report app |
| 2026-01-19 | Set OTEL Vault values for finance_report app (staging/production) |
| 2026-01-19 | Added restart-safe compose wiring and fixed Vault template quoting |
| 2026-02-02 | **Staging Investigation**: Found missing S3_PUBLIC_* in Vault, added them (v36) |
| 2026-02-02 | **Root Cause Found**: get_service_url() was adding env suffix to shared services |
| 2026-02-02 | **Fix Applied**: Updated libs/common.py to skip suffix for SHARED_PLATFORM_SERVICES |
| 2026-02-02 | PR #116 created: Skip environment suffix for MinIO/Vault/Dokploy/SSO public URLs |

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


## TODOWRITE (Archived)

# Infra-009 TODOWRITE

## Pending Tasks

- [ ] Create finance_report/finance_report/ structure
- [ ] Deploy PostgreSQL
- [ ] Deploy Redis
- [x] Deploy App
- [x] Verify end-to-end
- [x] Set OTEL Vault values for finance_report app (staging/production)
- [ ] Confirm logs appear in SigNoz UI
- [x] Connect finance_report app backend ERROR/FATAL logs as first live instance of shared SigNoz alert rule automation
- [ ] Eliminate the fixed-environment Traefik route gap observed during rollout
      (`v0.1.41`: about 10s in Staging and 40s in Production).

## Notes

Main documentation is in finance_report repository:
- EPIC-007.deployment.md

## Preview env (multi-alias, manual deploy) — P2 step 4c

- [x] Preview alias model: `tools/deploy_env_config.py::preview_alias(kind, value)` —
      pure (kind,value) -> {env_suffix, domain_suffix, app_url, compose slug, telemetry label}.
- [x] Preview compose template with bundled ephemeral postgres + DATABASE_URL override:
      `finance_report/finance_report/preview/compose.yaml`.
- [x] Lifecycle backend `libs/deploy/preview.py` (`up` / `down`) over the existing
      Dokploy client (find-or-create compose, deploy, health-check; teardown deletes volumes),
      driven through the `tools/deploy_v2` front door (`--type preview/*` to bring up, `--down`
      to tear down) — the backend is no longer a standalone CLI.
- [x] Unit tests (mocked Dokploy + HTTP): `libs/tests/test_deploy_env_config.py` (alias model),
      `libs/tests/test_preview_lifecycle.py` (orchestration call order/args).
- [x] SSOT: `docs/ssot/core.environments.md` §4.6 — 3 manual deploy targets + preview alias
      table + ephemeral-DB / explicit-teardown contract; telemetry §4.5 extended for aliases.
- [ ] **LIVE smoke (needs real Dokploy)**: compose.create payload accepted; ephemeral DB
      boots + migrations run against it; an alias routes end-to-end at report-<alias>/api/health.

## Artifacts

- Added OTEL keys to finance_report app secrets template
- Documented OTEL keys in finance_report app README
- Added `IAC_CONFIG_HASH` to finance_report app compose for restart-safe updates
- Replaced unsupported template helpers in finance_report app secrets template
- Locked the finance_report app rate-limit fallback to staging `2000` and
  production `300`, with Vault retaining precedence (finance_report#1829).
- Locked the fixed app backend healthcheck to the same 450-second bounded cold-start
  budget as preview after staging needed 177 seconds to become healthy.
- Scoped finance_report Vault app-token policies by environment and added targeted token repair/revoke tracking for `vault.setup-tokens`. _(Historical: this static-token machinery was retired in #369 — every service is now on AppRole.)_
- First app alert path: `finance-report-backend` -> OTEL -> SigNoz `FinanceReportBackendErrorLogs` -> `platform/12.alerting` -> Feishu/Lark

## v0.1.41 Deployment Evidence (2026-07-16)

- Release coordinate: `v0.1.41` -> `bf2ddcece2edaa66026d9f4cebae74967ed428f3`.
- Source CI: `29433332683`; release images: `29433836031`.
- First Staging run `29433897739` deployed successfully through receiver
  `29433935768`, but failed closed on one critical UI visibility assertion after
  smoke, Tier-2 HTTP, and 28/29 core E2E tests passed.
- Full Staging retry `29434443339` succeeded, including provider and AI/OCR gates;
  receiver run: `29434482577`.
- Production dry-run `29438575349` succeeded without mutation.
- Production release `29438700400` and receiver `29438752870` succeeded; health,
  infrastructure smoke, shell smoke, and read-only E2E passed; rollback was not invoked.
- Final public health reported `v0.1.41` in both Staging and Production. The rollout
  exposed transient 404 route gaps of about 10 seconds and 40 seconds respectively,
  retained above as a zero-downtime follow-up.
