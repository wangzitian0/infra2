# Infra-021: OpenPanel Installation

**Status**: In Progress  
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
