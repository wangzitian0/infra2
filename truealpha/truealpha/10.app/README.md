# TrueAlpha Application (web + llm)

Two containers from the truealpha app repo's GHCR images
(`ghcr.io/wangzitian0/truealpha-app-web`, `ghcr.io/wangzitian0/truealpha-llm-service`):

- **web** — Next.js (standalone) at `truealpha${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}`.
  Reads the Postgres `mart` schema directly (the app's init.md rule 5) via the
  Vault-rendered `DATABASE_URL` (`app_service_login`, `APP_SERVICE_DB_PASSWORD`);
  migrations run with `MIGRATIONS_DATABASE_URL` (`postgres`). There is no web→llm API hop.
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

The data engine (`../20.data_engine/`) exports OTLP HTTP to `127.0.0.1:14318` as `truealpha-data-engine` (`../20.data_engine/compose.yaml`).

## Deploy model

`app_pinned` release promotion: `harness/repos.yaml` pins the `release_tag`
for `truealpha/app`, and `deploy_v2 --service truealpha/app --type staging
--version-ref vX.Y.Z --iac-ref vA.B.C --domain zitian.party` reconciles Dokploy
compose through `iac_runner` (`invoke ta-app.sync`).

## Deploy

```bash
export VAULT_ROOT_TOKEN=$(op read 'op://Infra2/bootstrap/vault/Root Token/Root Token')
DEPLOY_ENV=<env> invoke vault.setup-approle --project=truealpha --service=app
python -m tools.deploy_v2 --service truealpha/app --type staging --iac-ref vX.Y.Z --domain zitian.party
invoke ta-app.shared.status
curl https://truealpha-staging.truealpha.club/api/health
```
