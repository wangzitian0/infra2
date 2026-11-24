# Archived Projects: Provenance Index

**SSOT Type**: Provenance index of completed Infra projects
**Scope**: One entry for each closed `Infra-NNN` project. Each entry gives the outcome and the PR, issue and tag links.

## Rules

- This file is the only record of a closed project in `docs/project/archive/`.
- To close a project, add one entry here and delete the project file and its TODOWRITE file in the same PR.
- An entry has a `### Infra-NNN: Title` heading, a `**Status**` line, an `**Outcome**` line and a `**Links**` line.
- Keep this file at 25 KiB or less. `common/tests/test_project_lifecycle_governance.py` enforces the rules.
- The SSOT under [`docs/ssot/`](../../ssot/README.md) holds the current contracts. An entry records history only.

## Full text

Issue #1826 compacted the long campaign files into this index.
The full text of each file is in git history at commit
[`a77e6f06`](https://github.com/wangzitian0/infra2/tree/a77e6f0679b70a548c64d65125719c393eb1ad79/docs/project/archive).
Read one file with `git show a77e6f06:docs/project/archive/<file>.md`.

## Entries

<a id="infra-021"></a>

### Infra-021: OpenPanel Installation

- **Status**: Archived — Completed
- **Outcome**: OpenPanel analytics runs on the Dokploy host on the shared Postgres, Redis and ClickHouse, with Vault secrets. SSOT: [`platform.openpanel.md`](../../ssot/platform.openpanel.md).
- **Links**: [#266](https://github.com/wangzitian0/infra2/pull/266) (durable host bind mount for op-ch), [#268](https://github.com/wangzitian0/infra2/pull/268) (isolate op-ch by env-suffixed name).
- **Source file**: `Infra-021.openpanel_install.md`

<a id="infra-019"></a>

### Infra-019: Workspace Harness Control Plane

- **Status**: Archived — Completed (epic #820 closed)
- **Outcome**: infra2 is a verifiable workspace harness: `harness/repos.yaml` inventory, read-only `harness check` and `harness status`, the `oh-my-code-agent` submodule, and the three-party boundary in [`core.harness.md`](../../ssot/core.harness.md) §5.1.
- **Links**: epic [#820](https://github.com/wangzitian0/infra2/issues/820); [#699](https://github.com/wangzitian0/infra2/pull/699), [#757](https://github.com/wangzitian0/infra2/pull/757) (`doc_link_check`), [#760](https://github.com/wangzitian0/infra2/pull/760), [#762](https://github.com/wangzitian0/infra2/pull/762); infra2-sdk [#31](https://github.com/wangzitian0/infra2-sdk/pull/31) released as [`v1.5.2`](https://github.com/wangzitian0/infra2-sdk/releases/tag/v1.5.2); truealpha [#821](https://github.com/wangzitian0/truealpha/pull/821).
- **Source file**: `Infra-019.harness_control_plane.md`

<a id="infra-018"></a>

### Infra-018: Repository Boundary Decoupling

- **Status**: Archived
- **Outcome**: The apps dropped their infra2 `repo/` submodules and consume the released `infra2-sdk`. infra2 pins the SDK and the apps under `repos/` as development snapshots only.
- **Links**: infra2-sdk [#1](https://github.com/wangzitian0/infra2-sdk/pull/1) released as [`v0.1.0`](https://github.com/wangzitian0/infra2-sdk/releases/tag/v0.1.0); infra2 [#488](https://github.com/wangzitian0/infra2/pull/488), [#489](https://github.com/wangzitian0/infra2/pull/489), [#490](https://github.com/wangzitian0/infra2/pull/490), [#491](https://github.com/wangzitian0/infra2/pull/491); truealpha [#217](https://github.com/wangzitian0/truealpha/pull/217); finance_report [#1878](https://github.com/wangzitian0/finance_report/pull/1878), [#1880](https://github.com/wangzitian0/finance_report/pull/1880).
- **Source file**: `Infra-018.repository_boundary_decoupling.md`

<a id="infra-016"></a>

### Infra-016: CI Gate Inventory

- **Status**: Archived — Closed (#460 and #461 completed and frozen; epic #459 closed as not planned)
- **Outcome**: infra2 CI has a coordinate inventory, [`ci-gate-inventory.yaml`](../../ssot/ci-gate-inventory.yaml), and a fail-closed audit, `tools/ci_gate_audit.py`. The app-side phases 3 and 4 moved into boundary governance (finance_report#876).
- **Links**: [#280](https://github.com/wangzitian0/infra2/issues/280), [#459](https://github.com/wangzitian0/infra2/issues/459), [#460](https://github.com/wangzitian0/infra2/issues/460), [#461](https://github.com/wangzitian0/infra2/issues/461), [#465](https://github.com/wangzitian0/infra2/issues/465); finance_report [#876](https://github.com/wangzitian0/finance_report/issues/876), [#1435](https://github.com/wangzitian0/finance_report/issues/1435), [#1491](https://github.com/wangzitian0/finance_report/issues/1491), [#1492](https://github.com/wangzitian0/finance_report/issues/1492).
- **Source file**: `Infra-016.ci_gate_inventory.md`

<a id="infra-015"></a>

### Infra-015: deploy_v2 — the unified deploy front door

- **Status**: Archived — Completed and live-verified
- **Outcome**: `deploy_v2` is the one deploy path, addressed by `(service, type, version_ref, iac_ref)`. `deploy.yml` keeps only the IaC Runner bootstrap. The contract is in [`ops.pipeline.md`](../../ssot/ops.pipeline.md) §4.
- **Links**: [#354](https://github.com/wangzitian0/infra2/pull/354), [#355](https://github.com/wangzitian0/infra2/pull/355), [#356](https://github.com/wangzitian0/infra2/pull/356), [#357](https://github.com/wangzitian0/infra2/pull/357), [#358](https://github.com/wangzitian0/infra2/pull/358), [#359](https://github.com/wangzitian0/infra2/pull/359), [#361](https://github.com/wangzitian0/infra2/pull/361), [#363](https://github.com/wangzitian0/infra2/pull/363), [#365](https://github.com/wangzitian0/infra2/pull/365), [#366](https://github.com/wangzitian0/infra2/pull/366), [#371](https://github.com/wangzitian0/infra2/pull/371) (closes [#370](https://github.com/wangzitian0/infra2/issues/370)); finance_report [#1072](https://github.com/wangzitian0/finance_report/issues/1072), [#1173](https://github.com/wangzitian0/finance_report/pull/1173).
- **Source file**: `Infra-015.deploy_v2_front_door.md`

<a id="infra-014"></a>

### Infra-014: finance_report Observability Wiring

- **Status**: Archived — Completed
- **Outcome**: The finance_report backend and browser send OTel traces to the one SigNoz collector. The browser uses the public ingest `otel.${INTERNAL_DOMAIN}` with a CORS allowlist. Each environment has its own OpenPanel client id.
- **Links**: [#360](https://github.com/wangzitian0/infra2/pull/360). Contract: [`ops.observability.md`](../../ssot/ops.observability.md) §4.3 and §4.4.
- **Source file**: `Infra-014.finance_report_observability.md`

<a id="infra-011"></a>

### Infra-011: Reliability and CI/CD Stage Contract

- **Status**: Archived — Completed (#158, #162, #168, #182, #183 closed; the AC table below stays because tests cite `Infra-011.x` ids)
- **Outcome**: Deploys wait for the real IaC Runner result, preflight checks fail early, and CI, watchdog and probe outputs share one Env x Stage result schema.
- **Links**: [#158](https://github.com/wangzitian0/infra2/issues/158), [#162](https://github.com/wangzitian0/infra2/issues/162), [#168](https://github.com/wangzitian0/infra2/issues/168), [#182](https://github.com/wangzitian0/infra2/issues/182), [#183](https://github.com/wangzitian0/infra2/issues/183), [#543](https://github.com/wangzitian0/infra2/issues/543); follow-ups [#186](https://github.com/wangzitian0/infra2/issues/186), [#187](https://github.com/wangzitian0/infra2/issues/187), [#189](https://github.com/wangzitian0/infra2/issues/189), [#191](https://github.com/wangzitian0/infra2/issues/191), [#610](https://github.com/wangzitian0/infra2/issues/610), [#945](https://github.com/wangzitian0/infra2/issues/945), [#1477](https://github.com/wangzitian0/infra2/issues/1477); last full production reconcile tag [`v1.1.48`](https://github.com/wangzitian0/infra2/releases/tag/v1.1.48); infra2-sdk [`v0.3.0`](https://github.com/wangzitian0/infra2-sdk/releases/tag/v0.3.0).
- **Source file**: `Infra-011.reliability_hardening.md`

<a id="infra-010"></a>

### Infra-010: IaC & Service Verification

- **Status**: Archived — Completed 2026-01-24
- **Outcome**: The IaC Runner image installs the 1Password CLI and `unzip`, gets AppRole credentials, and passes health checks. The push-to-deploy trigger it recorded is retired; see [`ops.pipeline.md`](../../ssot/ops.pipeline.md) §2 and §3.
- **Links**: [#74](https://github.com/wangzitian0/infra2/pull/74), [#101](https://github.com/wangzitian0/infra2/pull/101), [#102](https://github.com/wangzitian0/infra2/pull/102), [#153](https://github.com/wangzitian0/infra2/pull/153), [#154](https://github.com/wangzitian0/infra2/pull/154), [#166](https://github.com/wangzitian0/infra2/issues/166).
- **Source file**: `Infra-010.iac_and_service_verification.md`

<a id="infra-009"></a>

### Infra-009: Finance Report Deployment

- **Status**: Archived — Completed (superseded by deploy_v2, Infra-015)
- **Outcome**: finance_report runs on the platform with its own Postgres and Redis, shared MinIO and Vault-rendered secrets. The app-side EPIC is finance_report `EPIC-007.deployment.md`.
- **Links**: [#67](https://github.com/wangzitian0/infra2/pull/67), [#116](https://github.com/wangzitian0/infra2/pull/116) (S3 endpoint fix), [#369](https://github.com/wangzitian0/infra2/issues/369) (AppRole migration); finance_report [#1829](https://github.com/wangzitian0/finance_report/issues/1829) (rate-limit fallback), [`v0.1.41`](https://github.com/wangzitian0/finance_report/releases/tag/v0.1.41) (first release through the receiver, 2026-07-16).
- **Source file**: `Infra-009.finance_report_deploy.md`

<a id="infra-008"></a>

### Infra-008: Platform Multi-Environment (Staging)

- **Status**: Archived — Completed
- **Outcome**: Platform services deploy to staging and production in parallel through Dokploy environments. [`core.environments.md`](../../ssot/core.environments.md) defines the environments.
- **Links**: [#42](https://github.com/wangzitian0/infra2/pull/42) (merged 2026-01-03).
- **Source file**: `Infra-008.platform_multi_env.md`

<a id="infra-007"></a>

### Infra-007: SigNoz Observability Platform Installation

- **Status**: Archived — Completed
- **Outcome**: ClickHouse (`platform/03.clickhouse`) and SigNoz (`platform/11.signoz`) run with the OTLP collector inside the Docker network only. The out-of-band watchdog ACs below stay because tests cite them.
- **Links**: no PR recorded. Contract: [`ops.observability.md`](../../ssot/ops.observability.md) §4.
- **Source file**: `Infra-007.signoz_install.md`

<a id="infra-005"></a>

### Infra-005: Homer Portal + SSO Protection

- **Status**: Archived — Completed (PR #28)
- **Outcome**: The Homer portal (`platform/21.portal`) runs behind Traefik forward auth to Authentik. Only the `admins` group can open it.
- **Links**: [#28](https://github.com/wangzitian0/infra2/pull/28).
- **Source file**: `Infra-005.homer_portal.md`

<a id="infra-004"></a>

### Infra-004: Authentik Installation

- **Status**: Archived — Completed (PR #28)
- **Outcome**: Authentik SSO runs on Postgres and Redis with vault-init. The deployer framework `common/deploy/deployer.py` started here.
- **Links**: [#28](https://github.com/wangzitian0/infra2/pull/28).
- **Source file**: `Infra-004.authentik_install.md`

<a id="infra-003"></a>

### Infra-003: Documentation Reorganization

- **Status**: Archived
- **Outcome**: The docs have one navigation index, a project catalog and a MkDocs site that builds with no warnings.
- **Links**: [#10](https://github.com/wangzitian0/infra2/pull/10).
- **Source file**: `Infra-003.docs_reorg.md`

<a id="infra-002"></a>

### Infra-002: 1Password + Vault Installation

- **Status**: Archived
- **Outcome**: 1Password Connect and Vault run and hold the platform secrets.
- **Links**: pre-history; no PR recorded.
- **Source file**: `Infra-002.1password_vault_install.md`

<a id="infra-001"></a>

### Infra-001: Bootstrap Base Setup

- **Status**: Archived
- **Outcome**: The L1 bootstrap layer runs: host set-up, DNS and certificate groundwork, and the runtime directory layout.
- **Links**: pre-history; no PR recorded.
- **Source file**: `Infra-001.bootstrap_setup.md`

## Acceptance criteria that tests cite

Tests in `common/tests/` cite these ids. `common/tests/test_docs_lint.py` reads this table. Each proof path must exist.

| AC | Criterion | Proof |
|---|---|---|
| Infra-007.1 | The out-of-band watchdog runs on a daily schedule and on manual dispatch. | `.github/workflows/ops-checks.yml`, `common/tests/test_out_of_band_watchdog.py` |
| Infra-007.2 | Host checks run outside infra2 and alert through a direct Feishu webhook. | `tools/out_of_band_watchdog.py`, `common/tests/test_out_of_band_watchdog.py` |
| Infra-007.3 | The external workflow checks the alert bridge over SSH. The bridge stays private. | `.github/workflows/ops-checks.yml`, `tools/out_of_band_watchdog.py` |
| Infra-011.1 | A deploy waits for the real IaC Runner sync result and fails on a failed sync. | `common/tests/test_iac_runner_deploy_result.py`, `.github/workflows/deploy.yml` |
| Infra-011.2 | Each P1 dependency and each unhealthy Docker state has an owned watchdog signal. | `common/tests/test_infra_probes.py`, `common/tests/test_cloudflare_watchdog.py`, `common/tests/test_bootstrap_health.py`, `common/tests/test_vault_unsealer.py`, `common/tests/test_iac_runner_deploy_result.py`, `common/tests/test_out_of_band_watchdog.py`, `common/tests/test_watchdog_consistency_audit.py`, `platform/12.alerting/compose.yaml`, `cloudflare/infra-watchdog/worker.js`, `docs/ssot/watchdog-signals.yaml`, `tools/watchdog_consistency_audit.py` |
| Infra-011.3 | The Vault Agent health check reads the token, the rendered files and unresolved values. | `common/tests/test_vault_self_refresh_audit.py`, compose healthchecks |
| Infra-011.4 | Persistent data paths have backup inventory, a checksum runner and freshness checks. | `common/tests/test_backup_verification.py`, `tools/host_backup.sh`, `common/backup/emitter.py`, per-service `BackupFacet` declarations (derived inventory, #542) |
| Infra-011.5 | One owner routes each public service: a compose Traefik router or Dokploy, not both. | `common/tests/test_domain_routing_policy.py`, `docs/ssot/platform.domain.md` |
| Infra-011.6 | IaC Runner sync ensures each runtime secret field before the deploy. | `common/tests/test_deployer.py`, `platform/*/deploy.py` |
| Infra-011.7 | The IaC Runner health check covers required modules and binaries. | `common/tests/test_iac_runner_deploy_result.py`, `bootstrap/06.iac_runner/webhook_server.py` |
| Infra-011.8 | A change to runner bootstrap files rebuilds the IaC Runner out of band before `/deploy`. | `common/tests/test_iac_runner_deploy_result.py`, `common/tests/test_deployer.py`, `.github/workflows/deploy.yml`, `scripts/deploy_iac_runner_bootstrap.sh` |
| Infra-011.9 | Superseded by #543: resident probes replace the hourly route canary. | `common/tests/test_out_of_band_watchdog.py`, `common/tests/test_infra_probes.py`, `tools/out_of_band_watchdog.py`, `.github/workflows/ops-checks.yml` |
| Infra-011.10 | Deploy control accepts only commit SHAs and signs each CI call with a nonce. | `common/tests/test_iac_runner_deploy_result.py`, `.github/workflows/deploy.yml`, `bootstrap/06.iac_runner/webhook_server.py`, `bootstrap/06.iac_runner/sync_runner.py` |
| Infra-011.11 | A Dokploy deployment record is the proof that a deploy applied. | `common/tests/test_deployer.py`, `common/deploy/deployer.py` |
| Infra-011.12 | Outputs separate environment from stage in one Env x Stage schema. | `docs/ssot/ops.pipeline.md`, `docs/ssot/ops.observability.md`, `common/tests/test_retired_deploy_canary.py` |
| Infra-011.13 | External dependencies fail in preflight, before expensive stages. | `common/tests/test_cloudflare_watchdog.py`, `common/tests/test_out_of_band_watchdog.py`, `.github/workflows/deploy.yml` |
| Infra-011.14 | Long CI/CD stages publish budget evidence and budget breaches. | `common/tests/test_iac_runner_deploy_result.py`, `.github/workflows/deploy.yml`, `bootstrap/06.iac_runner/webhook_server.py`, `bootstrap/06.iac_runner/sync_runner.py` |
| Infra-011.15 | Cross-stage disagreements have definitions and measures. | `common/tests/test_infra_probes.py`, `common/tests/test_cloudflare_watchdog.py` |
| Infra-011.16 | Automatic reconcile of `iac_pinned` services needs fan-out evidence. | `docs/ssot/ops.pipeline.md`, `tools/reconcile_iac_inputs.py`, `common/tests/test_iac_runner_deploy_result.py`, `common/tests/test_reconcile_iac_inputs.py`, `.github/workflows/reconcile-iac-inputs.yml`, `.github/workflows/infra-ci.yml` |
| Infra-011.17 | A weekly restore into a throwaway target proves the off-host backup. | `common/tests/test_backup_verification.py`, `tools/run_restore_rehearsal.py`, `docs/ssot/ops.recovery.md` |
| Infra-011.18 | AI merge authority is fail-closed and bound to the current PR head. | `AGENTS.md`, `docs/ssot/ops.pipeline.md`, `docs/ssot/ci-gate-inventory.yaml` |
| Infra-011.19 | IaC Runner operation identity is environment, exact ref and service set. | `docs/ssot/ops.pipeline.md`, `bootstrap/06.iac_runner/webhook_server.py`, `bootstrap/06.iac_runner/sync_runner.py`, `common/deploy/iac_runner_client.py`, `common/deploy/deployer.py`, `tools/dokploy_config_drift.py`, `common/tests/test_iac_runner_client.py`, `common/tests/test_iac_runner_deploy_result.py`, `common/tests/test_deployer.py`, `common/tests/test_dokploy_config_drift.py` |
| Infra-011.20 | JavaScript Actions run on Node.js 24 in every workflow. | `common/tests/test_workflow_reference_contract.py`, `.github/workflows/reconcile-iac-inputs.yml`, `docs/ssot/ops.pipeline.md` |
| Infra-011.21 | Reconcile keeps unknown apart from empty and follows the last promotion marker. | `common/tests/test_dns_drift_report.py`, `common/tests/test_dokploy_config_drift.py`, `common/tests/test_reconcile_iac_inputs.py`, `common/tests/test_out_of_band_watchdog.py`, `docs/ssot/ops.pipeline.md`, `docs/ssot/ops.standards.md` |
| Infra-011.22 | A preview deploy passes only after its own deployment record is done. | `common/tests/test_preview_lifecycle.py`, `common/tests/test_preview_teardown_convergence.py`, `common/tests/test_deploy_v2.py`, `common/tests/test_retired_deploy_canary.py`, `common/tests/test_sdk_contract_adoption.py`, `.github/workflows/ops-checks.yml`, `common/deploy/preview.py`, `finance_report/finance_report/preview/compose.yaml`, `docs/ssot/ops.pipeline.md` |
| Infra-014.1 | Backend telemetry reaches the one SigNoz collector in each environment. | `finance_report/finance_report/10.app/secrets.ctmpl`, `finance_report/finance_report/preview/secrets.ctmpl`, `docs/ssot/ops.observability.md` |
| Infra-014.2 | The browser sends OTLP through one public domain, `otel.${INTERNAL_DOMAIN}`. | `platform/11.signoz/compose.yaml`, `platform/11.signoz/deploy.py`, `platform/11.signoz/otel-collector-config.yaml`, `finance_report/finance_report/10.app/compose.yaml`, `finance_report/finance_report/preview/compose.yaml` |
| Infra-014.3 | OpenPanel has a client id for production, staging and preview. | `finance_report/finance_report/10.app/deploy.py`, `finance_report/finance_report/10.app/compose.yaml`, `finance_report/finance_report/preview/compose.yaml`, `docs/ssot/platform.openpanel.md` |
| Infra-014.4 | The shipped CLIs query telemetry and analytics. | `platform/11.signoz/shared_tasks.py` (`query_logs`, `list_services`), `docs/ssot/ops.observability.md`, `docs/ssot/platform.openpanel.md` |
