# TrueAlpha Data Engine and Dagster

This service deploys the exact reviewed `truealpha-data-engine` OCI digest as two
runtime roles:

- `dagster-webserver` exposes the Dagster UI on a host-loopback-only port.
- `dagster-daemon` is the sole recurring-run authority and launches capture runs.

Both roles use the same image digest, isolated environment credentials, the
environment's TrueAlpha Postgres, and a dedicated artifact directory. Dagster
run/event/schedule metadata is stored in Postgres's `dagster` schema. Staging is
bounded to the immutable TOPT 20-issuer/21-instrument canary; Production expansion
requires a separate approved release/scope and remains shadow until graduation.

## Network Boundary

OpenD listens only on host `127.0.0.1:11111`. The two Dagster roles therefore use
host networking; no OpenD proxy is created. The Dagster UI also binds only to host
loopback (`13001` in Staging, `13002` in Production), has no Traefik route, and is
reached through an SSH tunnel when needed. Postgres uses its existing loopback ports
(`15432` Staging, `15433` Production).

## Required Vault Fields

The Vault KV v2 API/template path is
`secret/data/truealpha/<environment>/data_engine`. With the Vault CLI, use the
logical path `secret/truealpha/<environment>/data_engine` (the CLI inserts the
`data/` segment).

- `DATA_ENGINE_IMAGE_DIGEST`: full `sha256:<64 hex>` digest from the accepted image workflow.
- `RELEASE_MANIFEST_ID`: content-addressed `release-manifest:<64 hex>` identifier.
- `CAPTURE_APPROVED_BY`: recorded scope approver/decision identifier.
- `SEC_USER_AGENT`, `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET`.
- Optional: `S3_REGION`, `OPENFIGI_API_KEY`, moomoo defensive budget/throttle fields, `GIT_COMMIT_SHA`.

## Deploy

```bash
DEPLOY_ENV=staging invoke vault.setup-approle --project=truealpha --service=data_engine
python -m tools.deploy_v2 \
  --service truealpha/data_engine \
  --type staging \
  --iac-ref "$(git rev-parse HEAD)" \
  --domain zitian.party \
  --code-reviewed
DEPLOY_ENV=staging invoke ta-data_engine.shared.status
```

## Proof

```bash
docker inspect -f '{{.Config.Image}}' truealpha-dagster-webserver-staging
docker inspect -f '{{.Config.Image}}' truealpha-dagster-daemon-staging
docker exec truealpha-dagster-daemon-staging dagster-daemon liveness-check
ssh -L 13001:127.0.0.1:13001 <vps>  # then open http://127.0.0.1:13001
```

The two image references must be byte-identical and digest-pinned. A missing digest,
release manifest, approval, source credential, daemon heartbeat, or runtime image match
blocks deployment.
