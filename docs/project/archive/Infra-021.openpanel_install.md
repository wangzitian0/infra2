# Infra-021: OpenPanel Installation
**Status**: Archived — Completed
**Owner**: Infra  
**Priority**: P1  
**Branch**: `codex/infra-012-openpanel-install`

## Goal
Successfully deploy OpenPanel Analytics on the Dokploy VPS using shared Postgres, Redis, and ClickHouse services, integrated with Vault for secrets management, and verified through E2E health check and event ingestion tests.

## Context
OpenPanel is an open-source product analytics platform that provides tracking and visualization. Installing it on our infrastructure enables privacy-centric event tracking for applications (such as Finance Report). We want to avoid spinning up redundant databases by integrating OpenPanel directly into our shared database tier (`platform-postgres`, `platform-redis`, `platform-clickhouse`).

## Scope
- [ ] Create the project files and registers
- [ ] Write the SSOT file `docs/ssot/platform.openpanel.md`
- [ ] Provision Postgres databases and users for OpenPanel
- [ ] Provision ClickHouse databases for OpenPanel
- [ ] Securely store database credentials in Vault
- [ ] Write the Service Directory files under `platform/24.openpanel/` (compose.yaml, deploy.py, vault-policy.hcl, secrets.ctmpl, shared_tasks.py)
- [ ] Set up Vault agent and secrets rendering
- [ ] Expose OpenPanel dashboard and API via Dokploy Traefik configuration (with /api path stripping)
- [ ] Verify deployment using manual and automated probes

## Deliverables
- Project files in `docs/project/`
- SSOT documentation in `docs/ssot/platform.openpanel.md`
- Service directory `platform/24.openpanel/`
- Configured Vault secrets and database schemas
- Dokploy application instance running OpenPanel
- Green status verification check

## PR Links
- Submodule: [infra2 PR #new](https://github.com/wangzitian0/infra2/pull/new/codex/infra-012-openpanel-install)
- Parent: [finance_report PR #new](https://github.com/wangzitian0/finance_report/pull/new/codex/infra-012-openpanel-install)

## Change Log
| Date | Change |
|------|--------|
| 2026-06-10 | Initialized project |

## Verification
- [ ] `invoke openpanel.shared.status` returns success
- [ ] `curl -fsSL https://openpanel.${INTERNAL_DOMAIN}/api/healthcheck` returns OK
- [ ] E2E tracking check validates event ingestion into ClickHouse

## References
- SSOT: [docs/ssot/platform.openpanel.md](../ssot/platform.openpanel.md)
- Service directory: [platform/24.openpanel/](../../platform/24.openpanel/)
- Upstream: [OpenPanel self-hosting docs](https://openpanel.dev/docs/self-hosting)


## TODOWRITE (Archived)

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
