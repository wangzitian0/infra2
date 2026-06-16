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
| `S3_PUBLIC_ENDPOINT` | Externally reachable S3 API endpoint for short-lived presigned URLs |
| `S3_PUBLIC_BUCKET` | Public-endpoint bucket alias; defaults to `S3_BUCKET` |
| `AI_PROVIDER` | AI provider identifier; defaults to `zai` |
| `ZAI_API_KEY` | Z.AI API key for GLM models |
| `AI_API_KEY` | Provider-neutral API key alias for non-Z.AI providers |
| `AI_BASE_URL` | Provider base URL; defaults to Z.AI |
| `AI_CHAT_COMPLETIONS_PATH` | Chat completions API path |
| `AI_LAYOUT_PARSING_PATH` | OCR/layout parsing API path |
| `AI_MODEL_CATALOG_SOURCE` | Model catalog source; use `configured` for the local GLM catalog |
| `PRIMARY_MODEL` | Primary text model; defaults to `glm-5.1` |
| `OCR_MODEL` | OCR-capable model; defaults to `glm-4.6v` |
| `VISION_MODEL` | Vision-capable model; defaults to `glm-4.6v` |
| `AI_JSON_TIMEOUT_SECONDS` | Provider JSON extraction timeout; defaults to `360` |
| `AI_JSON_MAX_TOKENS` | Provider JSON extraction output cap; defaults to `8192` |
| `AI_JSON_DISABLE_THINKING` | Disable GLM thinking for deterministic JSON extraction; defaults to `true` |
| `FALLBACK_MODELS` | Comma-separated fallback model list |
| `AI_DAILY_LIMIT_USD` | Daily AI budget setting consumed by the app configuration |
| `NEXT_PUBLIC_APP_URL` | Frontend URL used by backend-generated links |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | SigNoz OTLP HTTP endpoint (optional Vault override; otherwise rendered — see Observability) |
| `OTEL_SERVICE_NAME` | OTEL service name (optional Vault override) |
| `OTEL_RESOURCE_ATTRIBUTES` | OTEL resource attributes (optional Vault override) |

## Observability Wiring (Infra-014)

Backend and browser frontend both emit OpenTelemetry traces to the single shared
SigNoz collector; OpenPanel analytics uses a per-environment client-id. None of this
is hand-set per env — it is injected as config-as-code at deploy time. The shared
image stays environment-agnostic (promote-not-rebuild): FE OTLP and OpenPanel values
are runtime env, not build-time baked.

### Backend OTEL_* — rendered by `secrets.ctmpl` (`10.app/` and `preview/`)

Templated deterministically from the deploy context (a Vault value at
`secret/data/finance_report/<env>/app`, if set, still wins as an escape hatch):

| Var | Rendered default |
|-----|------------------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://platform-signoz-otel-collector:4318` (Docker-internal; all envs, no suffix) |
| `OTEL_SERVICE_NAME` | `finance-report-backend` |
| `OTEL_RESOURCE_ATTRIBUTES` | `deployment.environment=<alias>,service.version=<git sha>` |

`<git sha>` comes from `GIT_COMMIT_SHA` passed into the vault-agent container by the
deploy primitive (`unknown` fallback). For `preview/`, `<alias>` is the per-alias ENV
(`main` / `pr-<N>` / `commit-<sha7>`), not the secrets-source env — preview spans stay
filterable by environment while still sourcing app secrets from `PREVIEW_SECRET_ENV`.

### Frontend NEXT_PUBLIC_OTEL_* — runtime env in `compose.yaml` (`10.app/` and `preview/`)

The browser cannot reach the Docker-internal collector, so FE OTLP goes through the
single public ingest domain (see SigNoz / `ops.observability.md`):

| Var | Value |
|-----|-------|
| `NEXT_PUBLIC_OTEL_EXPORTER_OTLP_ENDPOINT` | `https://otel.${INTERNAL_DOMAIN}/v1/traces` (CORS-restricted + static bearer token, rate-limited at Traefik) |
| `NEXT_PUBLIC_DEPLOYMENT_ENVIRONMENT` | `${ENV}` (mirrors the backend `deployment.environment` so FE/BE spans correlate) |
| `NEXT_PUBLIC_GIT_SHA` | `${GIT_COMMIT_SHA}` |

### OpenPanel `OPENPANEL_CLIENT_ID` — per-env, selected in `deploy.py`

`deploy.py` `openpanel_clients` maps `ENV` → client-id (production / staging /
preview); `OPENPANEL_CLIENT_ID` is injected as runtime env in both compose files. The
`preview` id is a placeholder until the real OpenPanel project is minted (see Infra-014
RUNBOOK). Analytics queries use the shipped `common/observability/openpanel_query.py`
CLI with `secret/platform/<env>/openpanel/api_key`.

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
