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
| `otel-collector-config.yaml` | OpenTelemetry Collector configuration |
| `prometheus.yml` | Prometheus config for query-service |
| `deploy.py` | SigNozDeployer |
| `shared_tasks.py` | Health check status() |

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
│ frontend         │ ──ui──> https://signoz.${INTERNAL_DOMAIN}
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
1. Create data directory (`/data/platform/signoz/data`)
2. Set permissions (755)

## Domain

**URL**: `https://signoz.${INTERNAL_DOMAIN}`

Automatically configured via Traefik labels in compose.yaml.

## Data Path

`/data/platform/signoz/` (chmod 755)
- `data/` - SQLite database for metadata

## Containers

- **schema-migrator**: Initialize ClickHouse schema (one-time)
  - Restart: on-failure
- **query-service**: Backend API and query engine
  - Port: 8080 (internal)
  - Health: `/api/v1/health`
- **frontend**: Web UI dashboard
  - Port: 3301 (via Traefik)
  - URL: `https://signoz.${INTERNAL_DOMAIN}`
- **otel-collector**: OpenTelemetry Collector
  - Port: 4317 (OTLP gRPC), 4318 (OTLP HTTP)
  - Health: 13133 (health_check extension)

## Access

### Web UI
- **URL**: `https://signoz.${INTERNAL_DOMAIN}`
- **First-time setup**: Create admin account on first visit

### OTLP Endpoints (for instrumentation)
- **gRPC**: `<VPS_HOST>:4317`
- **HTTP**: `<VPS_HOST>:4318`

Example instrumentation (Node.js):
```javascript
const { NodeSDK } = require('@opentelemetry/sdk-node');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-grpc');

const sdk = new NodeSDK({
  traceExporter: new OTLPTraceExporter({
    url: 'grpc://<VPS_HOST>:4317',
  }),
});

sdk.start();
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
- Query service: `platform-signoz-query-service:8080/metrics`
- OTLP collector: `platform-signoz-otel-collector:8888/metrics`

## Used By

- All applications sending telemetry data via OTLP

## References

- [SigNoz Official Docs](https://signoz.io/docs/)
- [OpenTelemetry](https://opentelemetry.io/)
