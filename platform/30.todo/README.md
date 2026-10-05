# Canary Todo (Infrastructure Verification Tool)

Canary service at `todo.zitian.party`. It proves the platform's real runtime paths. The full contract is in [`docs/ssot/platform.canary_todo.md`](../../docs/ssot/platform.canary_todo.md).

## Overview

- **Service**: todo
- **Port**: 8000 (container) -> 443 (Traefik)
- **Subdomain**: `subdomain = None` (uses Authentik ForwardAuth proxy labels in `compose.yaml`)
- **Deployer**: `TodoDeployer` (`platform/30.todo/deploy.py`)
- **Watchdog Probes**: Exempt (`check_id="probes"`, self-proving via `/api/canary/status`)

## Validated Capabilities

1. **Redis** (`platform/02.redis`): `AUTH`, then `SETEX`, `GET` and `DEL` of a random value. `-NOAUTH` fails.
2. **Postgres** (`platform/01.postgres`): login as the read-only role `canary_ro`, then `SELECT 1`. An `E` reply fails.
3. **S3** (`platform/03.s3`): `PUT`, `GET` and `DELETE` of one object under `canary/` in bucket `platform-canary`.
4. **SigNoz** (`platform/11.signoz`): collector liveness. The canary also exports its own traces and logs.
5. **OpenPanel** (`platform/24.openpanel`): API liveness.
6. **Authentik** (`platform/10.authentik`): SSO liveness.

## Endpoints

- **Health (public)**: `GET /api/health` returns HTTP 200 JSON.
- **Canary status (internal)**: `GET /api/canary/status` returns the check matrix. The probe runner reads it over the Docker network. SSO users reach it through the protected router. It is not public.
- **Web UI (SSO)**: `GET /` is protected by Authentik ForwardAuth.

## Deploy

```bash
# once per environment, staging first (see the SSOT doc, section 4)
DEPLOY_ENV=<env> uv run invoke vault.setup-approle --project=platform --service=todo
uv run invoke todo.sync
uv run invoke todo.sso-setup
```
