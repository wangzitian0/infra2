# TrueAlpha PostgreSQL

Dedicated Postgres 16 for the TrueAlpha app (github.com/wangzitian0/truealpha).
Follows the `finance_report/01.postgres` pattern exactly: vault-agent sidecar
(AppRole) renders `POSTGRES_PASSWORD` from `secret/truealpha/<env>/postgres`;
data persists on the VPS host bind mount `/data/truealpha/postgres` (uid 70,
chmod 700), prepared by `Deployer._prepare_dirs()` before deploy.

- Internal hostname: `truealpha-postgres${ENV_SUFFIX}:5432`, DB `truealpha`.
- Not publicly routed (`traefik.enable=false`).
- Schemas (`raw` / `staging` / `mart` / `dagster` + KG tables) are owned by the
  app repo (`db/migrations/*.sql`, idempotent). The `../10.app` `llm` container now
  applies them on every boot (`db/apply_migrations.sh`, baked into the image,
  truealpha#428) before serving — no manual step needed after first boot anymore.
  The old manual command (`for f in db/migrations/*.sql db/roles.sql; do psql
  "$DATABASE_URL" -f "$f"; done`, run from the truealpha repo) still works standalone
  if you need to apply schema without bringing the app up.

## Deploy

```bash
invoke env.set POSTGRES_PASSWORD=<value> --project=truealpha --service=postgres
export VAULT_ROOT_TOKEN=$(op read 'op://Infra2/bootstrap/vault/Root Token/Root Token')
invoke vault.setup-approle --project=truealpha --service=postgres
python -m tools.deploy_v2 --service truealpha/postgres --type staging --iac-ref vX.Y.Z --domain zitian.party
invoke ta-postgres.shared.status
```
