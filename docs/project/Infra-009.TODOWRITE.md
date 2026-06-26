# Infra-009 TODOWRITE

## Pending Tasks

- [ ] Create finance_report/finance_report/ structure
- [ ] Deploy PostgreSQL
- [ ] Deploy Redis
- [ ] Deploy App
- [ ] Verify end-to-end
- [x] Set OTEL Vault values for finance_report app (staging/production)
- [ ] Confirm logs appear in SigNoz UI
- [x] Connect finance_report app backend ERROR/FATAL logs as first live instance of shared SigNoz alert rule automation

## Notes

Main documentation is in finance_report repository:
- EPIC-007.deployment.md

## Preview env (multi-alias, manual deploy) — P2 step 4c

- [x] Preview alias model: `tools/deploy_env_config.py::preview_alias(kind, value)` —
      pure (kind,value) -> {env_suffix, domain_suffix, app_url, compose slug, telemetry label}.
- [x] Preview compose template with bundled ephemeral postgres + DATABASE_URL override:
      `finance_report/finance_report/preview/compose.yaml`.
- [x] Manual lifecycle CLI `libs/deploy/preview.py` (`up` / `down`) over the existing
      Dokploy client (find-or-create compose, deploy, health-check; teardown deletes volumes).
- [x] Unit tests (mocked Dokploy + HTTP): `libs/tests/test_deploy_env_config.py` (alias model),
      `libs/tests/test_preview_lifecycle.py` (orchestration call order/args).
- [x] SSOT: `docs/ssot/core.environments.md` §4.6 — 3 manual deploy targets + preview alias
      table + ephemeral-DB / explicit-teardown contract; telemetry §4.5 extended for aliases.
- [ ] **LIVE smoke (needs real Dokploy)**: compose.create payload accepted; ephemeral DB
      boots + migrations run against it; an alias routes end-to-end at report-<alias>/api/health.

## Artifacts

- Added OTEL keys to finance_report app secrets template
- Documented OTEL keys in finance_report app README
- Added `IAC_CONFIG_HASH` to finance_report app compose for restart-safe updates
- Replaced unsupported template helpers in finance_report app secrets template
- Scoped finance_report Vault app-token policies by environment and added targeted token repair/revoke tracking for `vault.setup-tokens`. _(Historical: this static-token machinery was retired in #369 — every service is now on AppRole.)_
- First app alert path: `finance-report-backend` -> OTEL -> SigNoz `FinanceReportBackendErrorLogs` -> `platform/12.alerting` -> Feishu/Lark
