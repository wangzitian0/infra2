# Canary Todo SSOT

> **SSOT Key**: `platform.canary_todo`
> **Definition**: Canary Todo (`platform/30.todo`) is a synthetic service at `todo.zitian.party`. It proves that the platform's real runtime paths work: authenticated writes to Redis, a Postgres login, an S3 write, and telemetry export. The probe runner reads its status document, so a broken path pages or reports.

---

## 1. Source of Truth

| Dimension | Location | Role |
|-----------|----------|------|
| Service code and probes | `platform/30.todo/app.py` | Probes, status document, telemetry bootstrap |
| Container topology | `platform/30.todo/compose.yaml` | App container, vault-agent sidecar, Traefik routers |
| Image | `platform/30.todo/Dockerfile` | Python 3.11 image. The `infra2-sdk` pin matches `pyproject.toml`. |
| Deployer and facets | `platform/30.todo/deploy.py` | `TodoDeployer`: probe, signal, secrets, telemetry identity, Postgres role |
| Secret contract | `platform/30.todo/env.manifest.json` | Generates `secrets.ctmpl` and `vault-policy.hcl` |
| Tests | `libs/tests/test_todo_service.py`, `libs/tests/test_todo_canary_depth.py` | Behaviour, routing, and deployer contracts |

## 2. What Each Check Proves

`GET /api/canary/status` runs six checks in parallel. Each check has an 8 s deadline. A check that does not finish in time reports `fail`.

Each check reports one of three states:

| State | Meaning |
|-------|---------|
| `pass` | The path works. |
| `fail` | The path is broken. |
| `unconfigured` | A credential the check needs is missing. The check is red, but nothing is known to be broken. |

**Gating checks** (the canary's own write paths) decide `ok` and the HTTP status:

| Check | Passes when | `fail` when | `unconfigured` when |
|-------|-------------|-------------|---------------------|
| `redis` | `AUTH`, `PING`, `SETEX`, `GET` (same random value), `DEL` all succeed | `-NOAUTH`, `-WRONGPASS`, any other error reply, a different value on `GET` | No `REDIS_PASSWORD` |
| `postgres` | Login as role `canary_ro` and `SELECT 1` succeed | Any server `E` (ErrorResponse), a failed login | No role name or password |
| `s3` (alias `minio`) | `PUT`, `GET` (same body) and `DELETE` of `canary/<random>` in bucket `platform-canary` succeed | A denied or failed request, a different body | No bucket, access key or secret key |

**Informational checks** (`signoz`, `openpanel`, `authentik`) test HTTP liveness (2xx or 3xx). They appear in `checks`, but they never change `ok` or the HTTP status. Each has its own probe and severity, so an outage must not page a second time as `todo-canary-status`.

The document returns HTTP 200 only when every gating check passes. It returns HTTP 503 when a gating check is `fail` or `unconfigured`. The body starts with the verdict, because the probe runner keeps only the first 128 bytes as its observed text:

```json
{"ok": false, "unconfigured": ["s3"], "failed": [], "service": "platform/todo", ...
```

`failed` lists gating checks in state `fail`. `unconfigured` lists gating checks in state `unconfigured`. A missing credential never appears in `failed`.

**One run at a time.** Each run does real writes and opens a Postgres connection, and the role allows 5. The service runs one set of checks at a time. Other requests wait and get that result for up to 30 s. The 30 s limit is shorter than the 60 s probe interval, so every probe round sees a fresh run. `age_seconds` states how old the result is.

## 3. Surfaces and Consumers

| Surface | Who can reach it | Used by |
|---------|------------------|---------|
| `GET /api/health` | Public (Traefik router priority 100, exact path) | Container healthcheck, external probes |
| `GET /api/canary/status` | The Docker network, and SSO users through the protected router | Probe runner (`todo-canary-status`), the dashboard |
| `GET /` and the rest | SSO users only (Authentik ForwardAuth, priority 10) | People |

`/api/canary/status` is not public. It shows internal host names and raw errors, and it runs real probes.

**Probe consumer.** `TodoDeployer.probes` declares `todo-canary-status` (HTTP 200, severity `warning` = P2, timeout 15 s). The probe runner polls it every 60 s. Three red rounds raise an alert. A staging runner sends a report instead of a page. A signal entry for each environment derives from `TodoDeployer.signals`.

**Severity.** The probe ships at `warning` because it is new and stays red while an operator step is open (an `unconfigured` check). Raise it to `error` (P1) only after staging and production acceptance: every gating check passes in both environments, and the production canary has answered HTTP 200 for a full day. Change `ProbeFacet.severity` and the two frozen fixtures in the same PR.

## 4. Credentials

The vault-agent sidecar renders `/secrets/.env` from Vault. The app container sources it at start. Compose environment never carries these values.

| Variable | Source class | Origin | Operator action |
|----------|--------------|--------|-----------------|
| `REDIS_PASSWORD` | `runtime`, `provided_by` | Vault `platform/<env>/redis` key `password` | None |
| `CANARY_POSTGRES_PASSWORD` | `runtime` | Generated once into Vault `platform/<env>/todo` | None |
| `CANARY_S3_ACCESS_KEY`, `CANARY_S3_SECRET_KEY` | `human` | 1Password item `platform/<env>/todo`, copied to Vault on deploy | Create once per environment (below) |

The policy lets the canary read only `platform/<env>/todo` and `platform/<env>/redis`. It cannot read the Postgres or S3 root credentials.

**Postgres role.** `TodoDeployer.apply_secret_supply` runs on every sync. After the supply succeeds, it runs idempotent SQL as `postgres` over SSH. The SQL goes through stdin, so the password never appears in an argument list. The session first sets `log_min_error_statement = 'panic'`, `log_statement = 'none'` and `log_min_duration_statement = -1`. Without them, the server logs a failed `ALTER ROLE` with its password. The deployer also removes the password from any psql error it prints. The role has `LOGIN`, no superuser, no create rights, `CONNECTION LIMIT 5`, `default_transaction_read_only = on`, and no `GRANT`. `SELECT 1` needs only `CONNECT`, which `PUBLIC` holds. A rotated Vault password takes effect on the next sync.

**First deploy.** The secret supply restarts the vault-agent and the app container after a value changes. On the first deploy neither exists. `Deployer.apply_secret_supply` restarts only the consumers that exist, so the sync reaches the role creation. If the host cannot list its containers, the supply fails.

**One-time operator steps per environment** (staging first):

1. `DEPLOY_ENV=<env> uv run invoke vault.setup-approle --project=platform --service=todo` (needs `VAULT_ROOT_TOKEN`). It creates the AppRole and policy, and injects `VAULT_ROLE_ID` and `VAULT_SECRET_ID` into the Dokploy compose env.
2. On the VPS host, create the canary bucket and its scoped key: `DEPLOY_ENV=<env> uv run invoke s3.shared.create-app-bucket --bucket-name=platform-canary --access-key=<AK> --secret-key=<SK> --lifecycle-days=1`.
3. Store the same key: `uv run invoke env.set CANARY_S3_ACCESS_KEY=<AK> --project=platform --env=<env> --service=todo --credential-type=root_vars`, and the same for `CANARY_S3_SECRET_KEY`.

Without step 3 the `s3` check reports `unconfigured`, with the names of the missing variables. The probe stays red (P2) and the deploy still succeeds.

**Rollout order.** On a release tag, `platform/alerting` (directory 12) deploys before `platform/todo` (directory 30). For a short time the new probe reads the previous canary, so a staging report in that window is expected. Follow this order:

1. Staging operator steps 1 to 3.
2. Push the tag. Staging deploys.
3. Open `/api/canary/status` through SSO in staging. Confirm every gating check is `pass` and the `todo-canary-status` signal is green.
4. Production operator steps 1 to 3.
5. Confirm the production canary answers HTTP 200 through SSO.
6. Promote production (owner).

## 5. Telemetry

`TodoDeployer.telemetry_service_name = "platform-todo"`. `Deployer.sync` issues `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES`, the same way it does for `truealpha/app`. The compose adds `OTEL_EXPORTER_OTLP_ENDPOINT=http://platform-signoz-otel-collector:4318` and `ENV`.

At start, `configure_service_telemetry()` calls `infra2_sdk.runtime.otel.configure_telemetry(..., set_global=True)` when the endpoint is set. The service refuses to start when the endpoint is set without `OTEL_SERVICE_NAME` or `ENV`. Otherwise the data would land under `unknown_service`.

Each run emits one `canary.run` span with one `canary.check.<name>` child span per check, and logs the outcome. SigNoz separates the environments by `deployment.environment.name`.

## 6. Constraints

### Do
- Add a new platform capability as a new check in `app.py`. Prove a real write or login, not liveness.
- Keep the probe target, the compose container name, and the app route equal. `test_probe_target_matches_the_compose_container_port_and_app_route` guards this.
- Keep the Dockerfile `infra2-sdk` pin equal to `pyproject.toml`, and its OpenTelemetry pins equal to the SDK `otel` extra.

### Do not
- Do not route `/api/canary/status` without SSO. `test_only_the_health_path_is_served_without_sso` evaluates the real Traefik rules.
- Do not count an authentication challenge, a server error reply, or a liveness response as proof of a write path.
- Do not report a missing credential as `fail`, and do not hide it as `pass`. Use `unconfigured`.
- Do not let a non-canary dependency gate `ok`. Add it as an informational check.
- Do not give the canary a root credential.
