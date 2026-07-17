# Infra-021: TODOWRITE (OpenPanel Installation)

**Status**: Active  
**Owner**: Infra

## Purpose
Track top issues discovered during the project.

## Top Issues (Top 30)
- [ ] `platform/24.openpanel/deploy.py`: Ensure databases are created before starting OpenPanel
- [ ] `platform/24.openpanel/compose.yaml`: Configure Traefik labels correctly for path stripping
- [ ] `docs/ssot/platform.openpanel.md`: Document integration architecture and variables
- [ ] **staging OpenPanel** `platform-openpanel-vault-agent-staging` crash-loops with `VAULT_ROLE_ID and VAULT_SECRET_ID are required`: the staging AppRole creds were never provisioned (OpenPanel was prod-only before). Run `vault.setup-approle` for staging openpanel, then it can run isolated (see #268). Currently `docker stop`-ped to clear the alias collision during the prod fix.

## Resolved — "deployed but not usable" incident (2026-06-11)

OpenPanel containers were all `healthy` but it could not store/track analytics:
prod `op-ch` (its dedicated ClickHouse) had **0 tables**. Three compounding bugs:

1. **Volume not durable** — `op-ch` used a Dokploy-managed named volume, which is
   recreated with a new hash on redeploy and silently wiped the event schema.
   Fix: durable `${DATA_PATH}/op-ch` host bind mount (uid/gid 101), matching
   `platform/03.clickhouse`. → **#266**
2. **Migration tracker vs wiped volume** — `__code_migrations` (in Postgres,
   persistent) still marked the ClickHouse migrations as applied after the volume
   wipe, so `pnpm migrate:deploy` skipped them and never recreated the tables.
   Fix (one-time data repair): delete the 6 ClickHouse migration rows
   (`3-init-ch`, `4-add-sessions`, `5-add-imports-table`, `6-add-revenue-column`,
   `8-order-keys`, `10-add-session-replay`) so a redeploy re-runs them. The
   PG-only migrations (`1-settings`, `2-accounts`, `7`, `9` — they use
   `db.report`) are left intact.
3. **Network alias collision** — `&op-env` referenced ClickHouse as bare
   `http://op-ch:8123`. `op-ch` is the compose service name → a network-wide
   alias on the shared external `dokploy-network`, claimed by BOTH prod and
   staging op-ch (→ 2 IPs). Prod migrations landed the 18-table schema on
   **staging** op-ch. Fix: reference the unique `platform-openpanel-ch${ENV_SUFFIX}`
   container name. → **#268**

Outcome: prod `op-ch` has the full 18-table schema on the durable bind mount;
op-api targets the correct instance; dashboard serves. All prod steps went
through the IaC `/deploy` pipeline (only the tracker repair was a manual DB op).
