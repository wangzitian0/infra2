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
- [x] `platform/12.alerting/compose.yaml`: every `INFRA_PROBE_SPECS` row binds canonical `service_id`; CI checks registry agreement and bidirectional inventory coverage
- [x] `docs/ssot/watchdog-signals.yaml`: all configured internal signals represented; component→service identity is registry-derived and audited
- [x] `cloudflare/infra-watchdog/wrangler.toml`: target/heartbeat `service_id` is audited against registry-derived inventory identity
- [x] Cross-plane `ServiceIdentity v1`: IaC, runtime observation, OTEL and alert labels share one renderer and blocking CI audit
- [ ] Generate irreducible watchdog endpoint skeletons instead of committed duplication (follow-up; identity drift is already blocked)
- [ ] `platform/23.prefect/compose.yaml:114`: `platform-authentik-server` missing `${ENV_SUFFIX}` → staging hits prod Authentik (P2, latent bug)
- [ ] compose lint: flag `platform-<svc>` references that should carry `${ENV_SUFFIX}` (P2)
- [ ] `docs/ssot/ops.backup-inventory.yaml`: derive service+data_path skeleton (P3)
- [x] `docs/ssot/vault-self-refresh-inventory.yaml`: derived from Deployer `SecretsFacet` declarations and deleted (#542)
- [ ] `bootstrap/02.dns_and_cert/tasks.py`: `DEFAULT_RECORDS` derive from subdomains (P3)
- [ ] ENV_SUFFIX logic duplicated: `sync_runner.deploy_env_overrides` vs `libs/common.py` (P3)
- [ ] `docs/ssot/MANIFEST.yaml`: mark generated inventories as lockfiles, deploy.py as registry SSOT
