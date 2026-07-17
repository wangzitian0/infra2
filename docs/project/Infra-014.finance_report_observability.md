# Infra-014: finance_report Observability Wiring (OTel → SigNoz + OpenPanel per-env)

**Status**: In Progress
**Owner**: Infra
**Priority**: P1
**Branch**: `obs-finance-report-otel-wiring`

## Goal
finance_report's backend and browser frontend emit OpenTelemetry traces into the
single shared SigNoz collector — backend over the Docker-internal OTLP endpoint and
the browser over one public, CORS-restricted, token-gated ingest domain
(`otel.${INTERNAL_DOMAIN}`) — with every environment (production / staging / preview
aliases) separated purely by the `deployment.environment` resource attribute, and
each environment pointed at its own OpenPanel analytics project, all queryable via
the already-shipped CLIs.

## Context
SigNoz (Infra-007) and OpenPanel (Infra-021) are deployed, but finance_report was not
yet wired to them per environment. Two gaps had to close as config-as-code:

1. **Backend telemetry** had empty `OTEL_*` defaults, so spans never reached SigNoz
   unless an operator hand-set Vault values per env.
2. **Browser telemetry** had no path at all: the OTLP collector is Docker-network-only
   (4317/4318 are `expose`d, never published), which a browser cannot reach.
3. **OpenPanel** had production/staging client-ids but no `preview` project mapping.

Architecture is fixed and intentionally minimal:

- **One shared collector.** SigNoz is `prod_only` (no `ENV_SUFFIX`); preview, staging
  and production all ship to `platform-signoz-otel-collector:4318`. Environments are
  distinguished downstream by the `deployment.environment` resource attribute, not by
  per-env collectors or per-env routes. See
  [core.environments.md](../ssot/core.environments.md) telemetry-identity rules.
- **One public ingest domain, Dokploy-managed.** `otel.${INTERNAL_DOMAIN}` is the only
  public surface, forwarded to `:4318`. Both this domain and the SigNoz Web UI domain
  are registered through Dokploy (no hand-written Traefik labels in `compose.yaml`):
  the base deployer flow registers the Web UI from `subdomain="signoz"`, and
  `SigNozDeployer.composing()` registers the second domain via an extra
  `ensure_domains(..., service_name="otel-collector")` call. There is **no bearer
  token** — a browser cannot hold a secret, so the page-shipped token of the original
  #360 design was not a credential. Public ingest is instead protected by a CORS
  allowlist for the known FE origins plus collector-side limits (`memory_limiter`); an
  edge per-IP rate limit remains a documented TODO (as a Dokploy-managed middleware).
- **Promote-not-rebuild image.** FE OTLP config (`NEXT_PUBLIC_OTEL_*`) and OpenPanel
  client-ids are injected as runtime env read server-side, not baked at build time, so
  the same image is environment-agnostic across promotions.

## Scope
- [x] Backend `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_SERVICE_NAME` /
      `OTEL_RESOURCE_ATTRIBUTES` rendered per env in `secrets.ctmpl` (app + preview).
- [x] Frontend `NEXT_PUBLIC_OTEL_EXPORTER_OTLP_ENDPOINT` /
      `NEXT_PUBLIC_DEPLOYMENT_ENVIRONMENT` / `NEXT_PUBLIC_GIT_SHA` in compose
      (app + preview).
- [x] Public ingest domain `otel.${INTERNAL_DOMAIN}`: Dokploy-managed domain on the
      otel-collector (`:4318`), registered via `SigNozDeployer.composing()`; protected
      by a CORS allowlist + collector `memory_limiter` (no bearer token). The SigNoz
      Web UI domain is restored to Dokploy management (`subdomain="signoz"`).
- [x] OpenPanel per-env client-ids — add `preview` to the `openpanel_clients` map
      (placeholder UUID + RUNBOOK to mint the real project).
- [x] SSOT + module docs updated (`ops.observability.md`, `platform.openpanel.md`,
      `finance_report/.../10.app/README.md`).
- Out of scope: minting credentials (tokens, OpenPanel project ids) — config-as-code
  only; see RUNBOOK. The OpenPanel query CLI and SigNoz query tasks already shipped and
  are referenced, not recreated.

## Acceptance Criteria

| AC | Description | Proof |
|----|-------------|-------|
| Infra-014.1 | Backend telemetry reaches the single shared SigNoz collector per env: `secrets.ctmpl` (app + preview) renders non-empty `OTEL_EXPORTER_OTLP_ENDPOINT` (`http://platform-signoz-otel-collector:4318`), `OTEL_SERVICE_NAME` (`finance-report-backend`), and `OTEL_RESOURCE_ATTRIBUTES` carrying `deployment.environment=<alias>,service.version=<git sha>`, with a Vault escape hatch that still wins when set. Preview renders the per-alias ENV (`main`/`pr-<N>`/`commit-<sha7>`), not the secrets-source env. | `finance_report/finance_report/10.app/secrets.ctmpl`, `finance_report/finance_report/preview/secrets.ctmpl`, `docs/ssot/ops.observability.md` |
| Infra-014.2 | The browser frontend ingests OTLP through exactly one public domain `otel.${INTERNAL_DOMAIN}` → `platform-signoz-otel-collector:4318`. The domain is **Dokploy-managed** (registered by `SigNozDeployer.composing()` via `ensure_domains(..., service_name="otel-collector")`; no hand-written Traefik router/service/middleware labels in `compose.yaml`), and so is the SigNoz Web UI domain (`subdomain="signoz"` → `signoz:8080`). There is **no bearer token**: public ingest is gated by a CORS allowlist on the OTLP HTTP receiver covering the report FE origins plus a collector `memory_limiter`; the internal gRPC receiver stays unpublished. | `platform/11.signoz/compose.yaml`, `platform/11.signoz/deploy.py`, `platform/11.signoz/otel-collector-config.yaml`, `finance_report/finance_report/10.app/compose.yaml`, `finance_report/finance_report/preview/compose.yaml` |
| Infra-014.3 | OpenPanel analytics has a per-env client-id for production, staging and preview: the `openpanel_clients` map resolves a client-id for `env_name=preview` (placeholder until minted), and `OPENPANEL_CLIENT_ID` is injected as runtime env in app + preview compose. | `finance_report/finance_report/10.app/deploy.py`, `finance_report/finance_report/10.app/compose.yaml`, `finance_report/finance_report/preview/compose.yaml`, `docs/ssot/platform.openpanel.md` |
| Infra-014.4 | Both analytics and telemetry are queryable via the already-shipped CLIs: SigNoz logs/traces via `invoke signoz.shared.query-logs` / `invoke signoz.shared.list-services`, and OpenPanel events via the app repo's `common/observability/openpanel_query.py` (using `secret/platform/<env>/openpanel/api_key`). Documented, not reimplemented. | `platform/11.signoz/shared_tasks.py` (`query_logs`, `list_services`), `docs/ssot/ops.observability.md`, `docs/ssot/platform.openpanel.md` |

## Deliverables
- Per-env backend `OTEL_*` rendering in both `secrets.ctmpl` files.
- Per-env frontend `NEXT_PUBLIC_OTEL_*` in both compose files.
- Public ingest domain (Dokploy-managed) + CORS allowlist + collector `memory_limiter` on SigNoz (no bearer).
- `preview` OpenPanel client-id mapping in `deploy.py`.
- Updated SSOT docs and the app module README.

## PR Links
- Submodule: [infra2 PR](https://github.com/wangzitian0/infra2/pull/new/obs-finance-report-otel-wiring)

## Change Log
| Date | Change |
|------|--------|
| 2026-06-16 | Initialized project; config-as-code wiring + public ingest route + docs. |
| 2026-06-16 | Follow-up: both domains Dokploy-managed (dropped hand-written Traefik routers); removed unusable browser bearer; ingest now CORS allowlist + collector `memory_limiter`. |

## Verification
- [ ] `docker compose -f platform/11.signoz/compose.yaml config` (CI `validate-compose`) passes with `INTERNAL_DOMAIN` set.
- [ ] `ruff check platform/ finance_report/` passes (CI `lint-python`).
- [ ] `mkdocs build --config-file docs/mkdocs.yml` builds with this page in nav.
- [ ] Post-merge RUNBOOK executed (Vault token + OpenPanel client-id + redeploy) — see PR body.

## References
- [SSOT: ops.observability](../ssot/ops.observability.md)
- [SSOT: platform.openpanel](../ssot/platform.openpanel.md)
- [SSOT: platform.domain](../ssot/platform.domain.md)
- [SSOT: core.environments](../ssot/core.environments.md)
- [Infra-007 SigNoz](Infra-007.signoz_install.md) · [Infra-021 OpenPanel](Infra-021.openpanel_install.md)
- App module: [finance_report/.../10.app/README.md](../../finance_report/finance_report/10.app/README.md)
