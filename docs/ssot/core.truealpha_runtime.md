# TrueAlpha Runtime and Environment Isolation SSOT

> **SSOT Key**: `core.truealpha_runtime`
> **Core definition**: TrueAlpha real-source capture is an environment-isolated,
> digest-pinned Dagster workload whose only recurring-run authority is the Dagster daemon.

## 1. Source of Truth

| Concern | Owner |
|---|---|
| Service topology and network boundary | `truealpha/truealpha/20.data_engine/compose.yaml` |
| Deployment inputs and runtime verification | `truealpha/truealpha/20.data_engine/deploy.py` |
| Runtime secrets (KV v2 API/template path) | `secret/data/truealpha/<environment>/data_engine` |
| PostgreSQL environment endpoint | `truealpha/truealpha/01.postgres/deploy.py` |
| Application capture contracts and assets | `wangzitian0/truealpha` data-engine image |

## 2. Environment Contract

- Staging and Production use different Postgres instances, raw buckets/credentials,
  Vault paths, Dagster metadata rows, API ledgers, data directories, and loopback ports.
- Staging runs the immutable TOPT acceptance canary and sentinels.
- Production graduated (`2026-07-27`) and runs TOPT + QQQ scopes with a separately approved release. A Staging scope cannot run under `ENVIRONMENT=production`.
- The daemon is the sole recurring-run authority. Host scripts are diagnostics and
  cannot satisfy scheduled-run evidence.

## 3. Artifact and Configuration Binding

All three Dagster roles (`dagster-code-server`, `dagster-daemon`, and `dagster-webserver`)
must run the same full OCI digest. Deploy fails closed unless Vault supplies:

- `IMAGE_DIGEST` as `sha256:<64 lowercase hex>`;
- `RELEASE_ID` as `release-manifest:<64 lowercase hex>`;
- `CAPTURE_APPROVED_BY` as the recorded scope decision;
- all required source/object-storage credentials.

The deployer hashes the compose, entrypoint, Vault template/config/policy, public
environment coordinates, release ID, and image digest into a 64-character
`CONFIGURATION_SHA256`. The capture run freezes these values before making source calls.
Post-deploy verification compares each running container's configured image reference
to the promoted digest; recording a Dokploy config hash alone is insufficient.

## 4. Network and Storage Boundary

- OpenD remains host-only on `127.0.0.1:11111`; no bridge-facing proxy is permitted.
- All three Dagster roles use host networking. They reach the environment's
  loopback-only Postgres port (`15432` Staging, `15433` Production) and the loopback
  S3 API port (`19000` Staging, `19001` Production) served by the platform S3
  storage (`platform-s3`).
- Dagster UI binds only to host loopback (`13001` Staging, `13002` Production), has
  `traefik.enable=false`, and is accessed through an SSH tunnel.
- Dagster run/event/schedule metadata uses the environment Postgres `dagster` schema.
  Local SQLite storage is prohibited for deployed runs.
- `/data/truealpha/dagster[-<env>]` contains only compute logs and IO artifacts. Raw
  bytes remain in the immutable S3 bucket; normalized evidence and orchestration
  metadata remain in Postgres. The VPS/archive copy protects against operator mistakes,
  not as a substitute for those systems of record.

## 5. Resource Contract

Every container has an explicit memory ceiling and CPU weight. Staging and Production
use the same memory ceiling (`1536m` per Dagster role), so staging rehearses the
production run mix. Staging has the lower CPU weight (`512`; Production `1024`).
`20.data_engine/compose.yaml` is the one source of the memory ceiling; `deploy.py` sets the CPU weight. `dagster.yaml` caps `max_concurrent_runs: 2` to prevent OpenD/source budget contention.

## 6. The Proof

```bash
DEPLOY_ENV=staging invoke ta-data_engine.shared.status
docker inspect -f '{{.Config.Image}}' truealpha-dagster-webserver-staging
docker inspect -f '{{.Config.Image}}' truealpha-dagster-daemon-staging
docker inspect -f '{{.Config.Image}}' truealpha-dagster-code-server-staging
docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' truealpha-dagster-code-server-staging
```

Required assertions:

1. All three image references equal the accepted digest.
2. The daemon heartbeat is current, code-server is healthy, and schedule metadata persists after restart.
3. Postgres contains Dagster runtime tables only in `dagster`, never `public`.
4. OpenD, Postgres, and Dagster UI are unreachable on non-loopback host interfaces.
5. A missing digest/release/approval/credential or wrong `ENVIRONMENT` blocks execution.

## 7. Telemetry & Ops Surface Split (L0–L4)

- **L0–L2 (`infra2`):** Delivery/Watchdog (`StageRecord`, CI gates, availability ledger) and SigNoz OTel metrics/logs (`OTEL_SERVICE_NAME` runtime override preserved) stay in `infra2` (see [ops.observability.md](./ops.observability.md#12-宏观全局看板与门禁治理原则-macro-dashboard--gate-rationalization-1195)).
- **L3 (`truealpha` Admin & Ops):** Runtime operations (`/admin`), DataHub facts (`/admin/datahub`), and read-only contract/applicability settings (`/admin/settings`) read Postgres `ops.*` views (`ops.nightly_verdicts`, `ops.output_invariant_results`, `ops.pipeline_trigger_requests`, `ops.head_rollback_requests`) and `app_ops_reader`, governed independently from `mart_readonly` research queries and non-superuser `data_engine_runtime` worker execution. Every Dagster lane schedule resolves its `DefaultScheduleStatus` from `environment_contract.v1.json` (`live_vendor_calls=false` in preview keeps all schedules `STOPPED`).
- **L4 (`truealpha` Research & MCP):** Dual-tier MCP bearer auth separates full admin/trigger access (`MCP_BEARER_TOKEN`) from optional read-only mart access (optional readonly MCP token resolving to `PrincipalKind.MEMBER`).

## Used By

- `truealpha/truealpha/20.data_engine/README.md`
- `docs/project/Infra-017.truealpha_dagster_capture.md`
- TrueAlpha issues #27, #51, #53, #67, and #68
