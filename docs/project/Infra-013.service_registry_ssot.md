# Infra-013: Service Registry as Single Source of Truth

**Status**: In Progress
**Owner**: Infra
**Priority**: P1
**Branch**: `infra-013-service-registry-ssot` (PR 1)

## Goal
The `deploy.py` Deployer class of each service is the **single** registry of that
service's facts; every other per-service / per-environment list is **derived from
or audited against** it, so adding or changing a service cannot silently drift its
monitoring, routing, backup, or fan-out config.

## Context
A staging false alert (signoz/clickhouse probes targeting non-existent
`-staging` hosts, firing every 30 min — see PR infra2#307) exposed a structural
problem, not a one-off typo: the same service facts are hand-copied into **three
layers** of parallel lists with little or no audit linking them.

```
① truth:   platform/*/deploy.py attrs (subdomain / service_port / prod_only / data_path)
② re-registries (libs/common.py, NO audit): SERVICE_SUBDOMAINS, CONTAINERS,
            SHARED_PLATFORM_SERVICES; sync_runner.py ALL_SERVICES / SERVICE_TASK_MAP
③ downstream (hand-copied): watchdog-signals.yaml, INFRA_PROBE_SPECS, wrangler
            targets, ops.backup-inventory.yaml, vault-self-refresh-inventory.yaml,
            DNS DEFAULT_RECORDS
+ 99+ inline `platform-<svc>:port` strings in compose files (prefect missed
  ${ENV_SUFFIX} → staging Prefect auth hits PROD Authentik).
```
`pure duplication` (service name, container, subdomain, port, prod_only,
data_path) should be derived. `irreducible external data` (health paths, expected
HTTP codes, SLAs, backup/restore methods, SSH probe commands) should live ONCE on
the service class, then ride along the generated skeleton — not scattered in yaml.

## Scope (MECE, delivered as minimal PRs)
- [x] **P0 — base library + collapse the cleanest duplication** (PR 1, this branch)
  - `libs/service_registry.py`: read Deployer attrs once (AST), expose
    `all_services()`, `services_in_env(env)`, `shared_services()`, `subdomains()`.
  - Audit: `sync_runner.ALL_SERVICES` must equal `all_services()` (fail-closed).
- [ ] **P0.1 — reconcile common.py re-registries** (PR 2)
  - Make `SERVICE_SUBDOMAINS` / `SHARED_PLATFORM_SERVICES` derive from (or be
    audited against) deploy.py `subdomain` / `prod_only`. Resolve the role-vs-
    service keying mismatch deliberately.
- [ ] **P1 — generate watchdog config from the registry** (PR 3)
  - Add `health_path` + `expected_codes` to the Deployer class (the only external
    probe data). Generate INFRA_PROBE_SPECS + watchdog-signals.yaml skeleton +
    wrangler targets via `get_probe_targets(env)` / `get_public_routes(env)`; CI
    asserts `generated == committed`. Kills the original drift class structurally.
- [ ] **P2 — fix prefect ${ENV_SUFFIX} bug + compose lint** (PR 4, parallel)
  - Fix `platform/23.prefect/compose.yaml:114` (missing suffix → prod Authentik).
  - Lint: any `platform-<svc>` compose reference that should carry `${ENV_SUFFIX}`
    but does not → CI fails.
- [ ] **P3 — derive skeletons for backup / vault-refresh / DNS** (PR 5)
  - Generate the service-list skeleton; keep hand-authored external annotations.
- [ ] **P3 — de-dup ENV_SUFFIX logic** (PR 6)
  - One implementation (sync_runner vs common.py).
- [ ] **Docs** — MANIFEST.yaml: mark generated yaml inventories as lockfiles, not
  hand-authored SSOT; deploy.py becomes the registry SSOT.

## Out of scope
- Full templating / runtime rendering of compose hostnames (static YAML can't call
  Python; the P2 lint captures ~80% of the risk at a fraction of the cost).
- Moving irreducible external data out of the service class into a generator.

## Deliverables
- `libs/service_registry.py` base library (`get_*`-style accessors).
- Fail-closed audits binding each downstream list to the registry.
- Generated (not hand-authored) watchdog / inventory skeletons.

## PR Links
- PR 1 (P0): _this branch_ — base library + ALL_SERVICES audit.

## Change Log
| Date | Change |
|------|--------|
| 2026-06-14 | Initialized project; PR 1 delivers the base library + ALL_SERVICES audit |

## Verification ("The Proof")
- [x] `pytest libs/tests/test_service_registry.py` — registry derives, ALL_SERVICES matches
- [ ] `pytest libs/tests/test_watchdog_consistency_audit.py` stays green per PR
- [ ] Each later PR adds a fail-closed audit proving its list == registry-derived

## References
- [SSOT: watchdog.signals](../ssot/watchdog-signals.yaml)
- [SSOT: platform.automation](../ssot/platform.automation.md)
- PR infra2#307 — the staging false alert that motivated this project
