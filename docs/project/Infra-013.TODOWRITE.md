# Infra-013: TODOWRITE (Service Registry SSOT)

**Status**: Active
**Owner**: Infra

## Purpose
Track the duplication sites that must be derived-from / audited-against the
`libs/service_registry.py` registry, discovered during the Infra-013 review.

## Top Issues
- [x] `bootstrap/06.iac_runner/sync_runner.py`: `ALL_SERVICES` hand-copied — now audited == registry (PR 1)
- [ ] `libs/common.py`: `SERVICE_SUBDOMAINS` keyed by role vs deploy.py `subdomain` keyed by service — reconcile (P0.1)
- [ ] `libs/common.py`: `SHARED_PLATFORM_SERVICES` must equal `service_registry.shared_services()` — add audit (P0.1)
- [ ] `libs/common.py`: `CONTAINERS` vs compose `container_name:` — audit (P0.1)
- [ ] `bootstrap/06.iac_runner/sync_runner.py`: `SERVICE_TASK_MAP` superset of registry (incl. bootstrap) — audit (P0.1)
- [ ] `platform/12.alerting/compose.yaml`: `INFRA_PROBE_SPECS` — generate from registry + `health_path`/`expected_codes` (P1)
- [ ] `docs/ssot/watchdog-signals.yaml`: generate skeleton from registry (P1)
- [ ] `cloudflare/infra-watchdog/wrangler.toml`: `WATCHDOG_TARGETS_JSON` / `HEARTBEATS_JSON` — generate (P1)
- [ ] `platform/23.prefect/compose.yaml:114`: `platform-authentik-server` missing `${ENV_SUFFIX}` → staging hits prod Authentik (P2, latent bug)
- [ ] compose lint: flag `platform-<svc>` references that should carry `${ENV_SUFFIX}` (P2)
- [ ] `docs/ssot/ops.backup-inventory.yaml`: derive service+data_path skeleton (P3)
- [ ] `docs/ssot/vault-self-refresh-inventory.yaml`: derive service skeleton (P3)
- [ ] `bootstrap/02.dns_and_cert/tasks.py`: `DEFAULT_RECORDS` derive from subdomains (P3)
- [ ] ENV_SUFFIX logic duplicated: `sync_runner.deploy_env_overrides` vs `libs/common.py` (P3)
- [ ] `docs/ssot/MANIFEST.yaml`: mark generated inventories as lockfiles, deploy.py as registry SSOT
