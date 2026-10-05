# TrueAlpha Application (web + llm)

Two containers from the truealpha app repo's GHCR images
(`ghcr.io/wangzitian0/truealpha-app-web`, `ghcr.io/wangzitian0/truealpha-llm-service`):

- **web** — Next.js (standalone) at `truealpha${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}`.
  Reads the Postgres `mart` schema directly (the app's init.md rule 5) via the
  Vault-rendered `DATABASE_URL`; there is no web→llm API hop.
- **llm** — FastAPI at the same host under `PathPrefix(/api)` (stripped). Scope is
  LLM-call orchestration only: MCP endpoint first, `/chat` later (Tier 3).
- **data-engine is deployed separately** from `../20.data_engine/` so real-source
  scheduling, Vault scope, host-only OpenD access, resources, and promotion identity
  remain independent from the web/LLM serving plane.

## Telemetry

The llm container exports OTLP to the one shared SigNoz collector
(`OTEL_EXPORTER_OTLP_ENDPOINT=http://platform-signoz-otel-collector:4318`, Docker network
only) and takes `OTEL_SERVICE_NAME` (`truealpha-app`) and `OTEL_RESOURCE_ATTRIBUTES`
(`deployment.environment.name`, `infra.service.id`, `service.version`, `infra.iac.ref`)
from the deployment, never from Vault or a compose default. web gets none of it (Node, no
OTel SDK). Contract: `docs/ssot/ops.observability.md` 4.2.

The data engine (`../20.data_engine/`) is issued no telemetry: its Dagster roles run with
`network_mode: host`, where the collector's Docker DNS name does not resolve, and the
collector port is never published. With no endpoint the application-side exporter stays
off, so no identity is issued either; it needs a host-reachable ingest first.

## v1 deploy model (deliberate simplification)

`iac_pinned` like a platform service: `deploy_v2 --service truealpha/app --type
staging --iac-ref vX.Y.Z` → iac_runner → `invoke ta-app.sync` → Dokploy compose.
Images float on `${IMAGE_TAG:-latest}` (pushed by the app repo's CI on main).
The finance_report-style promote path (pinned tags, rollout verification,
per-PR previews) is deferred until there is real mart data to protect.

## Deploy

```bash
invoke env.set SEC_USER_AGENT='TrueAlpha research <email>' --project=truealpha --service=app
export VAULT_ROOT_TOKEN=$(op read 'op://Infra2/bootstrap/vault/Root Token/Root Token')
invoke vault.setup-approle --project=truealpha --service=app
python -m tools.deploy_v2 --service truealpha/app --type staging --iac-ref vX.Y.Z --domain zitian.party
invoke ta-app.shared.status
curl https://truealpha-staging.zitian.party/api/health
```
