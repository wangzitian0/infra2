# SigNoz (Observability Platform)

> **Category**: Portal & Observability (20-29)

Open-source observability platform for logs, metrics, and traces (OpenTelemetry-native).

## Dependencies

- `03.clickhouse` must be deployed and healthy
- Vault must be available

## Files

| File | Purpose |
|------|---------|
| `compose.yaml` | Docker Compose (schema-migrator + query-service + frontend + otel-collector) |
| `otel-collector-config.yaml` | OpenTelemetry Collector config template (rendered to `${DATA_PATH}/otel-collector-config.yaml`) |
| `prometheus.yml` | Prometheus config for query-service |
| `deploy.py` | SigNozDeployer |
| `shared_tasks.py` | Health check and trace testing |

## Architecture

```
┌──────────────────┐
│ schema-migrator  │ ──setup──> ClickHouse (tables)
└──────────────────┘

┌──────────────────┐
│ otel-collector   │ ──ingest──> OTLP (4317 gRPC, 4318 HTTP)
│                  │ ──export──> ClickHouse
└──────────────────┘

┌──────────────────┐
│ query-service    │ ──query──> ClickHouse
│                  │ ──store──> SQLite (metadata)
└──────────────────┘

┌──────────────────┐
│ frontend         │ ──ui──> https://signoz${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}
│                  │ ──api──> query-service:8080
└──────────────────┘
```

## Deployment

```bash
# Ensure ClickHouse is ready
invoke clickhouse.status

# Full setup
invoke signoz.setup

# Or step-by-step
invoke signoz.pre-compose
invoke signoz.composing
invoke signoz.post-compose

# Check status
invoke signoz.status
```

**pre-compose** will:
1. Create data directory (`${DATA_PATH}/data`)
2. Set permissions (755)
3. Render OTEL config to `${DATA_PATH}/otel-collector-config.yaml`

## Domain

**URL**: `https://signoz${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}`

Configured via Dokploy domain settings in `deploy.py` (compose.yaml only enables Traefik).

## Data Path

`${DATA_PATH}` (chmod 755; staging uses `/data/platform/signoz-staging`)
- `data/` - SQLite database for metadata
- `otel-collector-config.yaml` - Rendered OTEL collector config (env suffix applied)

## Containers

- **schema-migrator**: Initialize ClickHouse schema (one-time)
  - Restart: no (runs once)
- **signoz**: Combined query-service + frontend (v0.105.1+)
  - Port: 8080 (API + UI, via Traefik)
  - Health: `/api/v1/health`
  - URL: `https://signoz${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}`
- **otel-collector**: OpenTelemetry Collector
  - Port: 4317 (OTLP gRPC), 4318 (OTLP HTTP) - Docker network only
  - Health: 13133 (health_check extension)

## Access

### Web UI
- **URL**: `https://signoz${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}`
- **First-time setup**: Create admin account on first visit

### Admin Credentials (1Password)
- **Item**: `platform/signoz/admin` (non-production: `platform/signoz/admin-<env>`)
- **Auto-seed**: `invoke signoz.shared.ensure-admin` (runs during `signoz.setup`)
- **Default email**: `signoz-admin@<domain>` (prod), `signoz-admin-<env>@<domain>` (non-prod)
- If registration is disabled, manually reset the admin password and update the 1Password item.

### Reset Metadata (Staging)
```bash
# WARNING: Deletes users/dashboards/alerts in the target environment.
DEPLOY_ENV=staging uv run invoke signoz.shared.reset-metadata
```

### OTLP Endpoints (for instrumentation)

> **Note**: OTLP ports (4317/4318) are only accessible within the Docker network (`dokploy-network`).
> Applications must be deployed on the same network to send telemetry.

- **gRPC**: `platform-signoz-otel-collector${ENV_SUFFIX}:4317` (Docker network only)
- **HTTP**: `platform-signoz-otel-collector${ENV_SUFFIX}:4318` (Docker network only)

Example instrumentation (Python, from Docker network):
```python
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
exporter = OTLPSpanExporter(endpoint="http://platform-signoz-otel-collector${ENV_SUFFIX}:4318/v1/traces")
```

### Test OTLP Connectivity

```bash
# Send a test trace
invoke signoz.shared.test-trace

# With custom service name
invoke signoz.shared.test-trace --service-name=myapp
```

## Application Integration (Project/Env/Service)

All application telemetry config is stored in Vault using the `project/env/service` hierarchy
defined by the shared `libs/env` tooling. This keeps local development simple while production
uses secure, IaC-managed secrets.

**OTEL log export keys** (Vault, per service):
- `OTEL_EXPORTER_OTLP_ENDPOINT` (HTTP): `http://platform-signoz-otel-collector${ENV_SUFFIX}:4318`
- `OTEL_SERVICE_NAME`: e.g. `finance-report-backend`
- `OTEL_RESOURCE_ATTRIBUTES`: e.g. `deployment.environment=production`

### Finance Report Example

```bash
# Production (finance_report/production/app)
uv run invoke env.set OTEL_EXPORTER_OTLP_ENDPOINT=http://platform-signoz-otel-collector:4318 --project=finance_report --env=production --service=app
uv run invoke env.set OTEL_SERVICE_NAME=finance-report-backend --project=finance_report --env=production --service=app
uv run invoke env.set OTEL_RESOURCE_ATTRIBUTES=deployment.environment=production --project=finance_report --env=production --service=app

# Staging (finance_report/staging/app)
uv run invoke env.set OTEL_EXPORTER_OTLP_ENDPOINT=http://platform-signoz-otel-collector-staging:4318 --project=finance_report --env=staging --service=app
uv run invoke env.set OTEL_SERVICE_NAME=finance-report-backend --project=finance_report --env=staging --service=app
uv run invoke env.set OTEL_RESOURCE_ATTRIBUTES=deployment.environment=staging --project=finance_report --env=staging --service=app
```

### Verification

```bash
# SigNoz health
uv run invoke signoz.status

# OTLP connectivity (shared_tasks.py)
uv run invoke signoz.shared.test-trace --service-name=finance-report-backend
```

## Configuration

### OpenTelemetry Collector
- Receivers: OTLP (gRPC + HTTP), Prometheus (self-monitoring)
- Processors: batch, memory_limiter (1GB limit)
- Exporters: ClickHouse
- Extensions: health_check, pprof, zpages

### Query Service
- Storage: ClickHouse (traces, metrics, logs)
- Metadata: SQLite (`/var/lib/signoz/signoz.db`)
- Alertmanager: Built-in

## Monitoring

Self-monitoring via Prometheus scraping:
- SigNoz service: `platform-signoz${ENV_SUFFIX}:8080/metrics`
- OTLP collector: `platform-signoz-otel-collector${ENV_SUFFIX}:8888/metrics`

## Used By

- All applications sending telemetry data via OTLP

## References

- [SigNoz Official Docs](https://signoz.io/docs/)
- [OpenTelemetry](https://opentelemetry.io/)
