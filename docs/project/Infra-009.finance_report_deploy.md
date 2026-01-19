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
