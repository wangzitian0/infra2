# Infra-011 TODOWRITE

**Project**: Reliability and CI/CD Stage Contract
**Status**: In Progress

## Acceptance Criteria

| AC | Description | Proof |
|----|-------------|-------|
| Infra-011.1 | Deploy workflow reports the real IaC Runner sync result instead of async acceptance. | `libs/tests/test_iac_runner_deploy_result.py` |
| Infra-011.2 | Infra service probes and out-of-band Docker health checks stay quiet on success, alert on unhealthy/starting/restarting containers, run internal probes at minute-level cadence with consecutive-failure/recovery thresholds, keep Cloudflare route blocks separate from service-down failures, dedupe unchanged failures, renotify on interval, send recovery notifications, feed a 30-minute Cloudflare Workers watchdog for public route and heartbeat checks, run daily GitHub audit checks for Worker/VPS macro health, and enforce watchdog signal ownership through a consistency audit. | `libs/tests/test_infra_probes.py`, `libs/tests/test_cloudflare_watchdog.py`, `libs/tests/test_out_of_band_watchdog.py`, `libs/tests/test_watchdog_consistency_audit.py` |
| Infra-011.3 | Vault Agent Docker health avoids mtime false-unhealthy, rejects unresolved template values, and audit keeps stale-file detection. | `libs/tests/test_vault_self_refresh_audit.py` |
| Infra-011.4 | Backup inventory, archive/checksum generation, and freshness manifest verification are code-enforced. | `libs/tests/test_backup_verification.py` |
| Infra-011.5 | Compose-owned Traefik routing is not mixed with Dokploy-generated domain routing. | `libs/tests/test_domain_routing_policy.py` |
| Infra-011.6 | IaC Runner sync ensures every runtime secret field consumed by custom service templates before deploy. | `libs/tests/test_deployer.py` |
| Infra-011.7 | 1Password Connect bootstrap uses the canonical `infra2.0` credentials/token pair, stable `credential` field lookup, and bearer-auth initialization before health probes. | `libs/tests/test_bootstrap_health.py`, `libs/tests/test_vault_unsealer.py` |
| Infra-011.8 | Post-merge deployments externally rebuild IaC Runner when runner bootstrap files change, then call `/deploy` only after public health recovers. | `libs/tests/test_iac_runner_deploy_result.py`, `.github/workflows/deploy.yml` |
| Infra-011.9 | Dokploy dynamic route canary classifies missing canary configuration and platform deploy failures before app PR previews depend on them, and the out-of-band watchdog pages worker/deployment-record failures independently of app CI. | `libs/tests/test_dokploy_route_canary.py`, `libs/tests/test_out_of_band_watchdog.py` |
| Infra-011.10 | IaC Runner deploy control accepts only immutable SHAs, uses timestamped nonce signatures, redacts public deploy responses, and prevents root-token resolution through 1Password. | `libs/tests/test_iac_runner_deploy_result.py` |
| Infra-011.11 | Generic Dokploy deployer sync retries no-op `compose.deploy` calls with `compose.redeploy` and fails fast when no new runtime deployment record appears. | `libs/tests/test_deployer.py` |
| Infra-011.12 | CI/CD, watchdog, canary, and probe outputs separate environment from pipeline stage and share a sparse Env x Stage result schema; deploy_v2 Canary is the first live producer. | `libs/tests/test_pipeline_stage_contract.py`, `libs/tests/test_deploy_v2_canary.py`, `docs/ssot/ops.pipeline.md`, `docs/ssot/ops.alerting.md` |
| Infra-011.13 | External dependencies fail in preflight before expensive stages. | `libs/tests/test_pipeline_stage_contract.py`, `libs/tests/test_cloudflare_watchdog.py`, `libs/tests/test_out_of_band_watchdog.py`, `libs/tests/test_dokploy_route_canary.py` |
| Infra-011.14 | Long-running stages publish soft budget, hard deadline, elapsed duration, current stage age, and budget breach classification. | `libs/tests/test_pipeline_stage_contract.py`, `libs/tests/test_iac_runner_deploy_result.py` |
| Infra-011.15 | Cross-stage disagreements are deterministic, measurable records rather than operator interpretation. | `libs/tests/test_pipeline_stage_contract.py`, `libs/tests/test_infra_probes.py`, `libs/tests/test_cloudflare_watchdog.py` |
| Infra-011.16 | CI/CD acceleration is evidence-gated through the Env x Stage matrix and does not weaken production full-sync or environment protection. | `libs/tests/test_pipeline_stage_contract.py`, `docs/ssot/ops.pipeline.md` |
| Infra-011.17 | Off-host backup durability is rehearsed by restoring the latest verified artifact into an explicitly throwaway target and checking database invariants; live-looking production containers are refused by default. | `libs/tests/test_backup_verification.py`, `tools/backup_restore_rehearsal.py`, `docs/ssot/ops.recovery.md` |
| Infra-011.18 | AI merge authority is fail-closed and bound to the current PR head, merge-authority CI, resolved review, complete change contracts, safety proof, and owner approval; ordinary merge remains decoupled from staging, while merge-triggered apply paths require explicit high-risk approval. | `AGENTS.md`, `docs/ssot/ops.pipeline.md`, `docs/ssot/delivery-stages.yaml`, `docs/ssot/ci-gate-inventory.yaml` |
| Infra-011.19 | IaC deployment operations bind environment, exact ref, and normalized service set to one opaque ID; release fidelity uses exact deploy ref plus a secret-independent source fingerprint, while runtime fingerprint remains the idempotence gate. | `libs/tests/test_iac_runner_client.py`, `libs/tests/test_iac_runner_deploy_result.py`, `libs/tests/test_deployer.py`, `libs/tests/test_dokploy_config_drift.py`, `docs/ssot/ops.pipeline.md` |
| Infra-011.20 | Every infra2 workflow uses the supported Node.js 24 major for governed official JavaScript Actions, and a repository-wide contract test rejects future stale-major additions or regressions. | `libs/tests/test_workflow_reference_contract.py`, `.github/workflows/reconcile-iac-inputs.yml`, `docs/ssot/ops.pipeline.md` |

## Issue Mapping

- #182: deploy result correctness.
- #183: live infra service probes.
- #168: Vault Agent rendered-file health contract.
- #158: off-host backup inventory and restore proof path.
- #162: external/synthetic and backup freshness alert coverage.
- wangzitian0/finance_report#945: off-host restore rehearsal proof for finance production data durability.
- #186: IaC Runner route ownership drift blocked main deploy health checks.
- #187: IaC Runner deploy failed after health recovery because repo `platform/` shadowed Python stdlib `platform`.
- #189: IaC Runner deploy sync lacks Vault automation token after stdlib shadow fix.
- #191: IaC Runner sync should use its scoped Vault app token.
- TBD: Env x Stage contract and cross-stage consistency tracking.

## TODO

- [x] Create missing GitHub issues and claim existing P1 issues.
- [x] Add deploy result tests and synchronous `/deploy` behavior.
- [x] Add infra probe runner tests and compose service.
- [x] Update Vault Agent Docker health contract and tests.
- [x] Reject rendered `<no value>` in Vault Agent health and audit.
- [x] Ensure custom deployer runtime secrets during IaC Runner sync.
- [x] Add out-of-band Docker unhealthy/starting/restarting watchdog coverage.
- [x] Add backup inventory, runner, and manifest verifier tests.
- [x] Add routing ownership policy test and remove current mixed-mode offenders.
- [x] Run IaC Runner invoke tasks without `platform/` shadowing Python stdlib imports.
- [x] Use Dokploy Domains for simple IaC Runner and Wealthfolio public routes.
- [x] Resolve Vault root token inside IaC Runner sync tasks without putting it in GitHub Actions.
- [x] Prefer IaC Runner scoped Vault app token for sync secret reads.
- [x] Pin 1Password Connect bootstrap to the canonical `infra2.0` credentials/token pair.
- [x] Make Vault unsealer health initialize 1Password Connect with bearer auth before checking dependency status.
- [x] Add Dokploy dynamic route canary with fail-closed configuration, fast-fail deployment, Docker, Traefik, and public route diagnostics.
- [x] Wire Dokploy route canary into the GitHub out-of-band watchdog alert path.
- [x] Fail generic deployer syncs when Dokploy accepts a request but does not create a new runtime deployment record.
- [x] Add Cloudflare Workers watchdog for production/staging public routes and probe-runner heartbeat.
- [x] Add signal ownership inventory and watchdog consistency audit.
- [x] Upgrade the release reconcile workflow to Node.js 24 Actions and enforce repository-wide minimum majors.
- [x] Run full lint/test suite.
- [x] Open PR.

## Redesigned TODO

- [x] Define the shared Env x Stage result schema in Pipeline and Alerting SSOT.
- [x] Add contract tests for stage names, failure domains, duration fields, deadline fields, external dependency flags, and suppression reasons.
- [ ] Add `config-preflight` classification for Cloudflare Worker JSON/KV/secret/delivery-mode failures.
- [ ] Add GitHub fallback watchdog preflight classification for missing SSH and Feishu configuration.
- [ ] Add deploy workflow stage summary with resolve, bootstrap-detect, bootstrap-update, IaC health preflight, deploy start, and status poll durations.
- [ ] Add IaC Runner deploy status fields for current stage, stage age, per-service elapsed time, skipped reason, and budget breach classification.
- [ ] Extend infra probe heartbeat payload with probe group summaries so Cloudflare can distinguish runner liveness from probe failures.
- [ ] Extend route canary phase evidence with deadline and budget-breach fields.
- [ ] Define and test cross-stage disagreement records for internal health vs public route, heartbeat vs probe result, canary vs app readiness, and GitHub fallback vs Cloudflare route checks.
- [ ] Use Env x Stage evidence to propose safe acceleration only after fallback coverage is proven; keep app production manual, and allow `iac_pinned` production reconcile only through reviewed-main fan-out plus deploy_v2/hash-gate evidence.

## 2026-06-18 Off-host Restore Rehearsal

- Added `libs/backup_restore.py` and `tools/backup_restore_rehearsal.py` to turn verified backup manifests into guarded Postgres restore rehearsals.
- Added AC Infra-011.17 and SSOT recovery SOP coverage for weekly throwaway-target restore checks.

## 2026-06-10 Env x Stage Contract Drift Fix

- Added the Env x Stage evidence contract, later published as `infra2_sdk.delivery`; the original local compatibility module was retired by Infra-018.
- Added `libs/tests/test_pipeline_stage_contract.py` to prove required fields, preflight classification, budget classification, safe acceleration rules, and deterministic disagreement records.
- Extended SSOT governance so Infra project AC proof table paths fail CI when they point at missing tests, tools, workflows, or code anchors.
- `deploy_v2_canary` now emits the released `infra2-sdk v0.3.0` shape on healthy output and failure alerts. Remaining producer migration is deploy workflow summaries, IaC Runner status payloads, route canary phase deadlines, and watchdog/probe records.

## 2026-07-16 SDK v0.3.0 Canary Adoption

- Upgraded infra2 from the immutable `infra2-sdk v0.1.0` wheel to `v0.3.0` and equality-guarded the local delivery-stage mirror.
- Made deploy_v2 Canary success and fail-path alerts emit SDK `StageResult` evidence with standard failure domains and duration/run URL evidence.
- Hardened review findings: no-wait evidence is a reasoned skip, successful evidence records resolved code/IaC SHAs, and workflow contract tests parse YAML structure instead of slicing text.
- Kept alerting low-noise: no periodic synthetic page was restored; healthy delivery remains proven by readiness probes and report delivery.

## 2026-07-16 AI Merge Authority

- Replaced the unconditional AI merge ban with a fail-closed gate bound to owner approval of the current PR head.
- Required exact-head merge-authority CI, resolved review threads, complete documentation and safety contracts, and post-merge verification before tag or promotion.
- Removed the stale rule that required staging before every ordinary merge; staging remains mandatory for release promotion, while merge-triggered apply paths require separate explicit approval.

## 2026-07-17 Deployment and Configuration Identity

- Made the normalized service set part of the IaC Runner operation key and use the trigger-returned deployment ID for status polling; legacy env/ref-only polling fails closed when ambiguous.
- Split versioned, release-recomputable source identity from runtime/secret config identity and persist the exact checked-out SHA. Runtime-only config requires an explicit secret-free source builder.
- Config drift now proves source fingerprint provenance against its stored deploy ref, compares the fingerprint with the latest release, and is strict on real drift/detector/structural failures. Pre-migration deployments are reported as `legacy_identity` without false drift.
- Proof: `uv run python -m pytest -q libs/tests` (`916 passed`); focused regression (`106 passed`); Ruff, compileall, workflow YAML parsing, and `git diff --check` passed.
- Rollout remains pending: after merge/release, each selected service backfills `IAC_SOURCE_CONFIG_HASH`/`IAC_DEPLOY_REF` on its next normal reconcile; legacy rows remain explicit and non-blocking, so no mass restart is required. The scheduled clean-checkout `--self-check` is the release proof. Local self-check is intentionally invalid while hash-input files differ from `HEAD`.

## 2026-07-17 GitHub Actions Node.js 24 Baseline

- Upgraded the release reconcile workflow from `checkout@v4`, `setup-python@v5`, and `upload-artifact@v4` to the repository's Node.js 24 majors (`v7`, `v6`, `v7`).
- Added a repository-wide workflow contract test so newly introduced workflows cannot bypass the minimum major baseline.
- Proof: full `libs/tests` (`953 passed`), focused workflow/reconcile contracts (`22 passed`), Ruff, workflow YAML parsing, SSOT/document governance, and `git diff --check` passed.
