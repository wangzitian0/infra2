# Canary Todo (Infrastructure Verification Tool)

Canary service at `todo.zitian.party`. It proves the platform's real runtime paths. The full contract is in [`docs/ssot/platform.canary_todo.md`](../../docs/ssot/platform.canary_todo.md).

## Overview

- **Service**: todo
- **Port**: 8000 (container) -> 443 (Traefik)
- **Subdomain**: `subdomain = None` (uses Authentik ForwardAuth proxy labels in `compose.yaml`)
- **Deployer**: `TodoDeployer` (`platform/30.todo/deploy.py`)
- **Watchdog Probes**: `ProbeFacet("todo-canary-status")` polling `/api/canary/status` over the Docker network

## Validated Capabilities

1. **Redis** (`platform/02.redis`): `AUTH`, then `SETEX`, `GET` and `DEL` of a random value. `-NOAUTH` fails.
2. **Postgres** (`platform/01.postgres`): login as the read-only role `canary_ro`, then `SET LOCAL transaction_read_only = off` first, then `SELECT 1`, and a session-scoped `canary_todo_probe` temporary table `CREATE`/`INSERT`/`SELECT`/`DELETE` roundtrip. An `E` reply fails.
3. **S3** (`platform/03.s3`): `PUT`, `GET` and `DELETE` of one object under `canary/` in bucket `platform-canary`.
4. **SigNoz** (`platform/11.signoz`): collector liveness. The canary also exports its own traces, logs, and custom OpenTelemetry metrics (`canary.probe.duration_ms`, `canary.probe.outcomes`), with its SigNoz v5 dashboard in `dashboard.json`.
5. **OpenPanel** (`platform/24.openpanel`): API liveness.
6. **Authentik** (`platform/10.authentik`): SSO liveness.
7. **Startup & Runtime Governance**:
   - `env.manifest.json`: enforced fail-closed at startup via `validate_startup_environment()` (`assert_environment`).
   - `runtime-hooks.manifest.json`: 4-slot runtime verification hooks (`smoke`, `preview_gate`, `staging_gate`, `scheduled_audit`).
   - `IAC_MOUNT_SHA256_*`: mount fingerprints injected by `TodoDeployer.rendered_mount_fingerprints()` into `compose.yaml`.

## Endpoints

- **Health (public)**: `GET /api/health` returns HTTP 200 JSON including release identity fields `version` and `git_sha`.
- **Canary status (internal)**: `GET /api/canary/status` returns the check matrix. Each check is `pass`, `fail` or `unconfigured` (a missing credential). Only Redis, Postgres and S3 gate the HTTP status. SigNoz, OpenPanel and Authentik are informational. The probe runner reads it over the Docker network. SSO users reach it through the protected router. It is not public.
- **Web UI (SSO)**: `GET /` is protected by Authentik ForwardAuth.

## Deploy

```bash
# once per environment, staging first (see the SSOT doc, section 4)
DEPLOY_ENV=<env> uv run invoke vault.setup-approle --project=platform --service=todo
uv run invoke todo.sync
uv run invoke todo.sso-setup
```
