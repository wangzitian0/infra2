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
- Staging runs only the immutable TOPT acceptance canary: 20 issuers and 21 selected
  U.S. equity instruments from accession `000207169126012475`.
- Production uses a separately approved release/scope and remains shadow until the
  application repository records graduation. A Staging scope cannot run under
  `APP_ENV=production`.
- The daemon is the sole recurring-run authority. Host scripts are diagnostics and
  cannot satisfy scheduled-run evidence.

## 3. Artifact and Configuration Binding

Both `dagster-webserver` and `dagster-daemon` must run the same full OCI digest.
Deploy fails closed unless Vault supplies:

- `DATA_ENGINE_IMAGE_DIGEST` as `sha256:<64 lowercase hex>`;
- `RELEASE_MANIFEST_ID` as `release-manifest:<64 lowercase hex>`;
- `CAPTURE_APPROVED_BY` as the recorded scope decision;
- all required source/object-storage credentials.

The deployer hashes the compose, entrypoint, Vault template/config/policy, public
environment coordinates, release ID, and image digest into a 64-character
`CONFIGURATION_SHA256`. The capture run freezes these values before making source calls.
Post-deploy verification compares each running container's configured image reference
to the promoted digest; recording a Dokploy config hash alone is insufficient.

## 4. Network and Storage Boundary

- OpenD remains host-only on `127.0.0.1:11111`; no bridge-facing proxy is permitted.
- Only the two Dagster roles use host networking. They reach the environment's
  loopback-only Postgres port (`15432` Staging, `15433` Production).
- Dagster UI binds only to host loopback (`13001` Staging, `13002` Production), has
  `traefik.enable=false`, and is accessed through an SSH tunnel.
- Dagster run/event/schedule metadata uses the environment Postgres `dagster` schema.
  Local SQLite storage is prohibited for deployed runs.
- `/data/truealpha/dagster[-<env>]` contains only compute logs and IO artifacts. Raw
  bytes remain in the immutable S3 bucket; normalized evidence and orchestration
  metadata remain in Postgres. The VPS/archive copy protects against operator mistakes,
  not as a substitute for those systems of record.

## 5. Resource Contract

Every container has an explicit memory ceiling and CPU weight. Staging uses the lower
tier (`768m` per Dagster role, CPU shares `512`); Production defaults to `1536m` and
CPU shares `1024`. The deployment queue allows one capture run at a time to prevent
OpenD/source budget contention.

## 6. The Proof

```bash
DEPLOY_ENV=staging invoke ta-data_engine.shared.status
docker inspect -f '{{.Config.Image}}' truealpha-dagster-webserver-staging
docker inspect -f '{{.Config.Image}}' truealpha-dagster-daemon-staging
docker exec truealpha-dagster-daemon-staging dagster-daemon liveness-check
```

Required assertions:

1. Both image references equal the accepted digest.
2. The daemon heartbeat is current and schedule metadata persists after restart.
3. Postgres contains Dagster runtime tables only in `dagster`, never `public`.
4. OpenD, Postgres, and Dagster UI are unreachable on non-loopback host interfaces.
5. A missing digest/release/approval/credential or wrong `APP_ENV` blocks execution.

## Used By

- `truealpha/truealpha/20.data_engine/README.md`
- `docs/project/Infra-017.truealpha_dagster_capture.md`
- TrueAlpha issues #27, #51, #53, #67, and #68
