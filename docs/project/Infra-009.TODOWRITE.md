# Infra-009 TODOWRITE

## Pending Tasks

- [ ] Create finance_report/finance_report/ structure
- [ ] Deploy PostgreSQL
- [ ] Deploy Redis
- [x] Deploy App
- [x] Verify end-to-end
- [x] Set OTEL Vault values for finance_report app (staging/production)
- [ ] Confirm logs appear in SigNoz UI
- [x] Connect finance_report app backend ERROR/FATAL logs as first live instance of shared SigNoz alert rule automation
- [ ] Eliminate the fixed-environment Traefik route gap observed during rollout
      (`v0.1.41`: about 10s in Staging and 40s in Production).

## Notes

Main documentation is in finance_report repository:
- EPIC-007.deployment.md

## Preview env (multi-alias, manual deploy) — P2 step 4c

- [x] Preview alias model: `tools/deploy_env_config.py::preview_alias(kind, value)` —
      pure (kind,value) -> {env_suffix, domain_suffix, app_url, compose slug, telemetry label}.
- [x] Preview compose template with bundled ephemeral postgres + DATABASE_URL override:
      `finance_report/finance_report/preview/compose.yaml`.
- [x] Lifecycle backend `libs/deploy/preview.py` (`up` / `down`) over the existing
      Dokploy client (find-or-create compose, deploy, health-check; teardown deletes volumes),
      driven through the `tools/deploy_v2` front door (`--type preview/*` to bring up, `--down`
      to tear down) — the backend is no longer a standalone CLI.
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

## v0.1.41 Deployment Evidence (2026-07-16)

- Release coordinate: `v0.1.41` -> `bf2ddcece2edaa66026d9f4cebae74967ed428f3`.
- Source CI: `29433332683`; release images: `29433836031`.
- First Staging run `29433897739` deployed successfully through receiver
  `29433935768`, but failed closed on one critical UI visibility assertion after
  smoke, Tier-2 HTTP, and 28/29 core E2E tests passed.
- Full Staging retry `29434443339` succeeded, including provider and AI/OCR gates;
  receiver run: `29434482577`.
- Production dry-run `29438575349` succeeded without mutation.
- Production release `29438700400` and receiver `29438752870` succeeded; health,
  infrastructure smoke, shell smoke, and read-only E2E passed; rollback was not invoked.
- Final public health reported `v0.1.41` in both Staging and Production. The rollout
  exposed transient 404 route gaps of about 10 seconds and 40 seconds respectively,
  retained above as a zero-downtime follow-up.
