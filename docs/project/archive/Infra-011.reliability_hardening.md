# Infra-011: Reliability and CI/CD Stage Contract

> **Status**: Archived — Completed (#158, #162, #168, #182, #183 all closed; AC table kept — tests cite `Infra-011.x` ids)  
> **Issues**: #158, #162, #168, #182, #183  
> **Goal**: Make infra2 fail loudly, fail early, and explain every CI/CD,
> watchdog, canary, and probe failure with a shared stage contract, bounded
> time budget, and cross-stage consistency signal.

## Situation

The first Infra-011 scope closed important reliability gaps: post-merge deploys
now wait for real IaC Runner sync results, infra probes exist, the Cloudflare
watchdog is primary out-of-band coverage, the GitHub watchdog is retained for
SSH diagnostics, the route canary classifies Dokploy routing failures, and
Vault/backup/deployer checks are code-owned.

That scope was necessary but incomplete. It answered "will failures be caught?"
better than it answered "will failures be caught early, within the right time
budget, and classified consistently across CI, CD, watchdogs, probes, and
canaries?" The remaining reliability bottleneck is now pipeline observability
and stage contract drift:

- External dependency failures are not uniformly modeled as preflight stage
  results. Some paths fail early with a clear GitHub error, while others fail as
  Worker invocation errors, repeated SSH target failures, or late IaC task
  failures.
- Long-running tasks have local timeouts, but the system does not expose a
  shared budget table or per-stage duration summary. Operators can see that a
  deployment is slow, but not whether the slow segment is GitHub scheduling,
  IaC Runner health, deploy start, service sync, Dokploy convergence, route
  materialization, notification delivery, or alert dedupe.
- Stage names and failure domains are not yet a common contract. Route canary
  has phase evidence, infra probes have elapsed time, IaC Runner has failure
  summaries, Cloudflare watchdog has failure details, and GitHub workflows have
  step logs, but they cannot be compared as one pipeline timeline.
- Cross-stage disagreements are not explicitly defined. For example, an
  internal service probe passing while the Cloudflare public route fails should
  mean `public-route` failure, not contradictory health. A fresh probe heartbeat
  with failing service probes should mean the runner is alive and service health
  is bad, not that the heartbeat path is healthy enough to ignore service
  failures.

The original P1 reliability review found these hard gaps:

- GitHub Actions could mark deploys green after IaC Runner only accepted an
  async request.
- Core infra service probes existed mostly as alert catalog TODOs.
- Vault Agent Docker health used rendered-file mtime freshness, which had
  already produced live unhealthy sidecars.
- Vault Agent rendered files could contain `<no value>` when a Vault template
  referenced a missing field; sidecars still looked healthy until the app
  failed to source `/secrets/.env`.
- IaC Runner `.sync` only ensured the base deployer secret, so custom deployer
  runtime fields could remain missing unless someone ran manual setup.
- IaC Runner `/health` did not include runtime dependency checks, so a stale
  bootstrap image could accept deploys even when invoke startup would fail on a
  missing Python package.
- 1Password Connect and `vault-unsealer` could look healthy while Connect sync
  was still `TOKEN_NEEDED` or while the configured Connect API token returned
  401 on authenticated item reads.
- IaC Runner could have a rendered `OP_SERVICE_ACCOUNT_TOKEN` in `/secrets/.env`
  while the long-running process still had an empty environment value.
- IaC Runner bootstrap source changes were not part of the post-merge deploy
  trigger, so a merged runner fix could leave the live webhook image stale until
  someone manually rebuilt the Dokploy compose.
- Local build and mounted runtime artifacts were not part of the generic
  deployer hash, so a service could keep an old image or template when compose
  and env text stayed unchanged.
- Backup coverage was not code-enforced against deployer-owned `DATA_PATH`
  services.

## Redesigned Scope

The redesigned epic keeps the completed reliability hardening work and extends
it with a shared CI/CD stage contract. The MECE task split is:

| Slice | Owner Surface | Objective | Out of Scope |
|-------|---------------|-----------|--------------|
| External dependency preflight | GitHub Actions, IaC Runner, Cloudflare Worker, GitHub watchdog, route canary | Fail missing secrets, KV bindings, invalid JSON, unreachable control planes, and required runtime dependencies before expensive work starts. | Replacing Feishu, Cloudflare, GitHub Actions, or Dokploy as providers. |
| Long-task time budget | `deploy.yml`, IaC Runner sync, route canary, watchdogs, probe runner | Define soft and hard budgets, emit per-stage duration, and classify budget breaches separately from functional failures. | Making every deployment faster before measuring the slow stages. |
| Stage contract and failure taxonomy | Shared docs, tests, alert payloads, GitHub summaries, public deploy status | Normalize `source`, `environment`, `stage`, `target`, `status`, `duration_ms`, `deadline_ms`, `failure_domain`, `external_dependency`, and `suppressed_reason`. | Logging raw stdout/stderr, secrets, or provider response bodies beyond safe redacted snippets. |
| Cross-stage consistency | Cloudflare watchdog, infra probe heartbeat, public route probes, route canary, CD summaries | Define disagreement as a measurable state, not an operator guess. Internal health, public route health, heartbeat freshness, and deployment proof must be comparable. | Treating all disagreement as outage; some disagreement is an expected localization signal. |
| Acceleration after evidence | CI setup/cache, staging changed-service deployment, deploy summary triage | Speed up only after the previous slices identify safe acceleration points and fallback coverage. | Weakening production full-sync guarantees or environment protection. |

Dependencies:

- Stage schema and taxonomy must land before implementing cross-source
  consistency metrics.
- Per-stage durations must land before tightening timeouts or skipping work.
- App production deployment must remain manual and environment-protected.
  `iac_pinned` production reconcile may run automatically only from reviewed
  infra2 `main`, through `deploy_v2` red lines and the iac_runner config-hash gate.
  This "only from reviewed main" precondition is now **fail-closed enforced** by
  `assert_after_on_main` (AC Infra-011.16), not just policy.
- GitHub fallback watchdog remains fallback/manual diagnostics; Cloudflare
  remains the primary out-of-band watchdog.

## Acceptance Criteria

| AC | Description | Proof |
|----|-------------|-------|
| Infra-011.1 | GitHub Actions deployment waits for the real IaC Runner sync result, fails on failed service syncs, and runs invoke without repo path shadowing Python stdlib modules. | `libs/tests/test_iac_runner_deploy_result.py`, `.github/workflows/deploy.yml` |
| Infra-011.2 | P1 infra dependencies, authenticated 1Password Connect paths, IaC Runner process secrets, and generic Docker unhealthy/starting/restarting states have signal-owned watchdog coverage: internal probes run at minute-level cadence with consecutive-failure/recovery thresholds, Cloudflare checks public routes and probe heartbeats every 30 minutes, GitHub runs daily audit checks for Worker self-health and VPS macro health, public-route Cloudflare 1010 blocks are not classified as internal service outages, unchanged failures dedupe/renotify, recoveries notify once, and a consistency audit prevents unassigned signals, undocumented exclusions, stale monitors, and prod/staging drift. | `libs/tests/test_infra_probes.py`, `libs/tests/test_cloudflare_watchdog.py`, `libs/tests/test_bootstrap_health.py`, `libs/tests/test_vault_unsealer.py`, `libs/tests/test_iac_runner_deploy_result.py`, `libs/tests/test_out_of_band_watchdog.py`, `libs/tests/test_watchdog_consistency_audit.py`, `platform/12.alerting/compose.yaml`, `cloudflare/infra-watchdog/worker.js`, `docs/ssot/watchdog-signals.yaml`, `tools/watchdog_consistency_audit.py` |
| Infra-011.3 | Vault Agent Docker health checks token lookup, rendered-file presence, and unresolved template values, while mtime freshness remains an audit signal. | `libs/tests/test_vault_self_refresh_audit.py`, compose healthchecks |
| Infra-011.6 | IaC Runner sync ensures every runtime secret field consumed by custom service templates before deploy, creates missing Vault service paths when it has scoped write permission, and services without runtime secret templates explicitly opt out of the generic secret preflight. | `libs/tests/test_deployer.py`, `platform/*/deploy.py` |
| Infra-011.4 | Deployer-owned persistent data paths have backup inventory coverage, an archive/checksum runner, and manifest freshness verification. | `libs/tests/test_backup_verification.py`, `tools/backup_runner.py`, per-service `BackupFacet` declarations (derived inventory, #542) |
| Infra-011.5 | Public service routing ownership is single-source: compose-owned Traefik routers must not also use Dokploy domain generation. | `libs/tests/test_domain_routing_policy.py`, `docs/ssot/platform.domain.md` |
| Infra-011.7 | IaC Runner health checks include required runtime Python modules and binaries, missing dependency failures are classified, and optional audit inventory dependencies do not break invoke startup. | `libs/tests/test_iac_runner_deploy_result.py`, `bootstrap/06.iac_runner/webhook_server.py` |
| Infra-011.8 | Post-merge deployments externally rebuild IaC Runner through the VPS/Dokploy compose checkout before calling `/deploy` when runner bootstrap files change, disable Dokploy auto-deploy ownership for the runner, persist the target runner `GIT_SHA`, retry public runner health while Traefik/Cloudflare routing converges, and generic deployer hashes include local build/mount artifacts so code-backed infra services do not skip redeploys. | `libs/tests/test_iac_runner_deploy_result.py`, `libs/tests/test_deployer.py`, `.github/workflows/deploy.yml`, `scripts/deploy_iac_runner_bootstrap.sh` |
| Infra-011.9 | (Superseded by #543) The hourly synthetic-compose route canary is retired outright — no observation window. Its coverage moved to cheaper resident mechanisms: public-route reachability is declared per service via `PublicRouteFacet` and rendered into the probe runner; Dokploy control-plane and per-compose deploy status is consumed by the out-of-band watchdog's `run_dokploy_status_check`, which fails closed with a `configuration` domain when `DOKPLOY_API_KEY` is absent (the signature the canary used to own); deploy stalls page via the deploy-queue guard sidecar plugin. | `libs/tests/test_out_of_band_watchdog.py`, `libs/tests/test_infra_probes.py`, `tools/out_of_band_watchdog.py`, `.github/workflows/ops-checks.yml` |
| Infra-011.10 | IaC Runner deploy control accepts only immutable commit SHAs, uses timestamped nonce signatures for CI deploy/status calls, redacts child stdout/stderr from public deploy responses, and prevents runner subprocesses from resolving Vault root tokens through 1Password. | `libs/tests/test_iac_runner_deploy_result.py`, `.github/workflows/deploy.yml`, `bootstrap/06.iac_runner/webhook_server.py`, `bootstrap/06.iac_runner/sync_runner.py` |
| Infra-011.11 | Generic Dokploy deployer sync treats deployment records as the runtime apply proof: `compose.deploy` must produce a new running/done deployment record from Dokploy's compose deployment listing API, retries once with `compose.redeploy` on no-op deploys, and fails fast instead of reporting success when both attempts leave runtime stale. | `libs/tests/test_deployer.py`, `libs/deploy/deployer.py` |
| Infra-011.12 | CI/CD, watchdog, canary, and probe outputs separate environment from pipeline stage and share one sparse Env x Stage result schema with explicit stage names, failure domains, duration, deadline, external dependency flag, and suppression reason. The deploy_v2 Canary emits the released SDK shape on both health and alert paths. | `docs/ssot/ops.pipeline.md`, `docs/ssot/ops.observability.md`, `libs/tests/test_pipeline_stage_contract.py`, `libs/tests/test_deploy_v2_canary.py` |
| Infra-011.13 | External dependencies fail in preflight before expensive stages: GitHub secrets/vars, IaC Runner health dependencies, Cloudflare KV/secrets/config JSON, Dokploy API credentials/environment IDs, SSH diagnostics config, and Feishu delivery mode configuration are all classified as `configuration` or `external-dependency` failures. | `libs/tests/test_pipeline_stage_contract.py`, `libs/tests/test_cloudflare_watchdog.py`, `libs/tests/test_out_of_band_watchdog.py`, `.github/workflows/deploy.yml` |
| Infra-011.14 | Long-running CI/CD stages publish budget evidence: soft budget, hard deadline, elapsed duration, current stage age for in-progress deploys, and budget breach classification without exposing child stdout/stderr. | `libs/tests/test_iac_runner_deploy_result.py`, `libs/tests/test_pipeline_stage_contract.py`, `.github/workflows/deploy.yml`, `bootstrap/06.iac_runner/webhook_server.py`, `bootstrap/06.iac_runner/sync_runner.py` |
| Infra-011.15 | Cross-stage disagreements are defined and measurable: internal service healthy plus public route failed, heartbeat fresh plus probe group failed, heartbeat stale plus route healthy, and GitHub fallback host failure plus Cloudflare route pass all produce deterministic disagreement records. | `libs/tests/test_pipeline_stage_contract.py`, `libs/tests/test_infra_probes.py`, `libs/tests/test_cloudflare_watchdog.py` |
| Infra-011.16 | Acceleration decisions are evidence-gated through the Env x Stage matrix: selected `iac_pinned` services may reconcile automatically from reviewed infra2 `main` only when fan-out evidence identifies changed inputs and the Deployer config-hash gate proves no-op vs restart; app production remains manual and environment-protected. The "from reviewed main" precondition is **fail-closed enforced**: before any apply, `assert_after_on_main` resolves the promoted tag and refuses it unless reachable from `origin/main`, so a release tag cut on an unmerged/off-main feature branch cannot drive a real staging/prod deploy (the v1.1.16 incident). Production is never automatic — a tag push auto-applies **staging only** (soak), prod requires an explicit `--promote-prod` promotion (`commands_to_apply`) — and a PR-time `--dry-run` plan gate (`infra-ci.yml`) surfaces the fan-out and proves the reconcile plan builds before merge; `--dry-run` plans are exempt from the provenance guard. | `docs/ssot/ops.pipeline.md`, `tools/reconcile_iac_inputs.py`, `libs/tests/test_iac_runner_deploy_result.py`, `libs/tests/test_reconcile_iac_inputs.py`, `.github/workflows/reconcile-iac-inputs.yml`, `.github/workflows/infra-ci.yml` |
| Infra-011.17 | Off-host backup durability is rehearsed by restoring the latest verified artifact into an explicitly throwaway target and checking database invariants; live-looking production containers are refused by default. | `libs/tests/test_backup_verification.py`, `tools/backup_restore_rehearsal.py`, `docs/ssot/ops.recovery.md` |
| Infra-011.18 | AI merge authority is fail-closed and bound to the current PR head, merge-authority CI, resolved review, complete change contracts, safety proof, and owner approval; ordinary merge remains decoupled from staging, while merge-triggered apply paths require explicit high-risk approval. | `AGENTS.md`, `docs/ssot/ops.pipeline.md`, `docs/ssot/delivery-stages.yaml`, `docs/ssot/ci-gate-inventory.yaml` |
| Infra-011.19 | IaC Runner operation identity is `(environment, exact ref, normalized service set)` and `/deploy/status` follows the opaque ID returned by `/deploy`, so concurrent service sets cannot share cache/in-flight/status. Runtime idempotence remains `IAC_CONFIG_HASH`; release fidelity is independently proven by secret-free `IAC_SOURCE_CONFIG_HASH` plus exact `IAC_DEPLOY_REF`. | `docs/ssot/ops.pipeline.md`, `bootstrap/06.iac_runner/webhook_server.py`, `bootstrap/06.iac_runner/sync_runner.py`, `libs/iac_runner_client.py`, `libs/deploy/deployer.py`, `tools/dokploy_config_drift.py`, `libs/tests/test_iac_runner_client.py`, `libs/tests/test_iac_runner_deploy_result.py`, `libs/tests/test_deployer.py`, `libs/tests/test_dokploy_config_drift.py` |
| Infra-011.20 | Official JavaScript Actions run on supported Node.js 24 majors across every infra2 workflow, with one repository-wide contract test preventing stale-major additions and regressions. | `libs/tests/test_workflow_reference_contract.py`, `.github/workflows/reconcile-iac-inputs.yml`, `docs/ssot/ops.pipeline.md` |
| Infra-011.21 | Reconcile observations preserve unknown vs empty, production drift follows the last successful explicit promotion marker, runtime-only deployers expose a secret-free source identity, and Dokploy/runtime disagreement stays failed but is classified independently from outage. | `libs/tests/test_dns_drift_report.py`, `libs/tests/test_dokploy_config_drift.py`, `libs/tests/test_reconcile_iac_inputs.py`, `libs/tests/test_out_of_band_watchdog.py`, `docs/ssot/ops.pipeline.md`, `docs/ssot/ops.standards.md` |
| Infra-011.22 | A preview deploy reports success only after a deployment record created by its own trigger reaches a terminal-good state and every configured public surface proves the requested version. The singleton canary slot is serialized across workflow events, same-repo PR canaries test their exact head IaC, uncertain compose-create outcomes reconcile by deterministic alias before failing, cleanup is successful only after stable observed absence, and the bounded health budget covers measured fresh-DB cold startup. Its stage evidence uses the configured canary deadline and a hard budget breach cannot report pass. | `libs/tests/test_preview_lifecycle.py`, `libs/tests/test_preview_teardown_convergence.py`, `libs/tests/test_deploy_v2.py`, `libs/tests/test_deploy_v2_canary.py`, `libs/tests/test_sdk_contract_adoption.py`, `.github/workflows/ops-checks.yml`, `libs/deploy/preview.py`, `finance_report/finance_report/preview/compose.yaml`, `docs/ssot/ops.pipeline.md` |

## Counterfactual Requirements

| Counterfactual | Required Result |
|----------------|-----------------|
| Cloudflare Worker has invalid `WATCHDOG_TARGETS_JSON`. | A `config-preflight` stage failure is recorded and tested; it must not masquerade as a public route outage. |
| The old preview stack still returns HTTP 200 while the replacement is deploying. | The old response is ignored until a deployment record newer than this invocation reaches a terminal-good state; a later Dokploy error fails the operation. |
| The backend reports the requested version but the frontend is still old or never started. | Preview readiness remains failed because every configured public surface must independently report the requested version. |
| A fresh preview database makes migrations and application startup take longer than one minute. | The backend remains in its startup grace period for the measured cold path instead of being killed before it can become healthy. |
| Two workflow runs concurrently target the reserved `pr-999` slot. | One waits in a non-cancelling job concurrency group; canaries cannot delete or accept one another's evidence. |
| A PR changes the preview compose while its canary clones `main`. | The workflow records the exact head SHA as authority, passes `github.head_ref` only as a clone ref, and the front door proves both resolve to the same commit before Dokploy clones it. |
| `compose.create` times out after Dokploy has already created the deterministic alias. | The caller re-reads that exact project/environment/name, adopts the single created compose, and does not create a duplicate or report an unverified control-plane failure. |
| Dokploy acknowledges delete while a queued deploy can still retain the compose. | Cleanup remains failed until bounded polling observes the compose absent twice consecutively; failure output exposes `torn_down=false`. |
| A canary finishes functionally healthy after its declared hard deadline. | Stage evidence uses the CLI's configured deadline and the command exits red with the time-budget failure domain; `status=pass` and `budget_status=hard-breach` cannot coexist. |
| `WATCHDOG_STATE` KV binding is missing. | Heartbeat and dedupe state failures are classified as `configuration`, with a deterministic failure domain. |
| Feishu app bot secret is missing. | Notification delivery preflight fails before route failures are deduped as sent. |
| IaC Runner `/health` returns degraded because a Python module or binary is missing. | `deploy.yml` bootstrap fails during `iac-health-preflight`, not after starting `/deploy`. |
| `/deploy/status` remains `in_progress` beyond budget. | GitHub summary reports the current deployment stage age and hard-timeout breach. |
| Two callers deploy different services at the same environment/ref. | They receive different deployment IDs and can only poll their own service-set result. |
| A service injects runtime secrets that the GitHub drift runner cannot read. | Runtime fingerprint changes for idempotence, while source fingerprint remains release-recomputable and does not report false drift. |
| A service's source inputs are unchanged in a newer release. | Drift proves the stored source fingerprint against its older exact deploy ref, then accepts the equal release fingerprint without restarting the service merely to advance metadata. |
| Internal service probes pass but Cloudflare public route fails. | The system records a `public-route` disagreement, not an unknown service outage. |
| Probe heartbeat is fresh but reports `ok=false`. | Cloudflare reports the runner as alive and the probe group as failing. |
| Probe heartbeat is stale but public routes are healthy. | Cloudflare reports `heartbeat-stale` separately from route health. |
| Dokploy canary compose drifts from raw compose to Git provider source. | Route canary fails as `dokploy-compose-source-type` with source/status and latest deployment log evidence. |
| Dokploy accepts deploy but creates no running/done deployment record. | Route canary fails as `dokploy-worker-or-deployment-record` before public route probing. |
| Staging service code is unchanged. | Acceleration may skip expensive staging work only when the stage contract proves the skip and records `skipped_reason`. |

## Validation

Current reliability baseline validation:

```bash
uv run python -P -m pytest \
  libs/tests/test_iac_runner_deploy_result.py \
  libs/tests/test_infra_probes.py \
  libs/tests/test_cloudflare_watchdog.py \
  libs/tests/test_out_of_band_watchdog.py \
  libs/tests/test_watchdog_consistency_audit.py \
  libs/tests/test_deployer.py \
  libs/tests/test_backup_verification.py \
  libs/tests/test_vault_self_refresh_audit.py \
  libs/tests/test_domain_routing_policy.py \
  -q
```

Full redesigned-scope validation after Infra-011.12 through Infra-011.16 land:

```bash
uv run python -P -m pytest \
  libs/tests/test_iac_runner_deploy_result.py \
  libs/tests/test_infra_probes.py \
  libs/tests/test_cloudflare_watchdog.py \
  libs/tests/test_out_of_band_watchdog.py \
  libs/tests/test_pipeline_stage_contract.py \
  -q
```

Full validation before PR:

```bash
uv run ruff check .
uv run python -P -m pytest libs/tests -q
```

## TODOWRITE (Archived)

> Merged from `Infra-011.TODOWRITE.md` on 2026-09-15 when the project was archived (#713). Its condensed
> acceptance table is superseded by the full Acceptance Criteria table above and is not repeated. The
> unchecked "Redesigned TODO" items (stage-summary fields, heartbeat payload, budget fields) were not
> picked up by the closed tracking issues; they remain here as the historical record.

### Issue Mapping

- #182: deploy result correctness.
- #183: live infra service probes.
- #168: Vault Agent rendered-file health contract.
- #158: off-host backup inventory and restore proof path.
- #162: external/synthetic and backup freshness alert coverage.
- wangzitian0/finance_report#945: off-host restore rehearsal proof for finance production data durability.
- #186: IaC Runner route ownership drift blocked main deploy health checks.
- #187: IaC Runner deploy failed after health recovery because repo `platform/` shadowed Python stdlib `platform`.
- #610: preview and dependent-service deployments can report success while the replacement later fails or remains `Created`.
- #189: IaC Runner deploy sync lacks Vault automation token after stdlib shadow fix.
- #191: IaC Runner sync should use its scoped Vault app token.
- TBD: Env x Stage contract and cross-stage consistency tracking.

### TODO

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
- [x] Harden observation truth, production target identity, secret-free source identity, and control/runtime discrepancy classification.
- [x] Run full lint/test suite.
- [x] Open PR.

### Redesigned TODO

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

### 2026-06-18 Off-host Restore Rehearsal

- Added `libs/backup_restore.py` and `tools/backup_restore_rehearsal.py` to turn verified backup manifests into guarded Postgres restore rehearsals.
- Added AC Infra-011.17 and SSOT recovery SOP coverage for weekly throwaway-target restore checks.

### 2026-06-10 Env x Stage Contract Drift Fix

- Added the Env x Stage evidence contract, later published as `infra2_sdk.delivery`; the original local compatibility module was retired by Infra-018.
- Added `libs/tests/test_pipeline_stage_contract.py` to prove required fields, preflight classification, budget classification, safe acceleration rules, and deterministic disagreement records.
- Extended SSOT governance so Infra project AC proof table paths fail CI when they point at missing tests, tools, workflows, or code anchors.
- `deploy_v2_canary` now emits the released `infra2-sdk v0.3.0` shape on healthy output and failure alerts. Remaining producer migration is deploy workflow summaries, IaC Runner status payloads, route canary phase deadlines, and watchdog/probe records.

### 2026-07-16 SDK v0.3.0 Canary Adoption

- Upgraded infra2 from the immutable `infra2-sdk v0.1.0` wheel to `v0.3.0` and equality-guarded the local delivery-stage mirror.
- Made deploy_v2 Canary success and fail-path alerts emit SDK `StageResult` evidence with standard failure domains and duration/run URL evidence.
- Hardened review findings: no-wait evidence is a reasoned skip, successful evidence records resolved code/IaC SHAs, and workflow contract tests parse YAML structure instead of slicing text.
- Kept alerting low-noise: no periodic synthetic page was restored; healthy delivery remains proven by readiness probes and report delivery.

### 2026-07-16 AI Merge Authority

- Replaced the unconditional AI merge ban with a fail-closed gate bound to owner approval of the current PR head.
- Required exact-head merge-authority CI, resolved review threads, complete documentation and safety contracts, and post-merge verification before tag or promotion.
- Removed the stale rule that required staging before every ordinary merge; staging remains mandatory for release promotion, while merge-triggered apply paths require separate explicit approval.

### 2026-07-17 Deployment and Configuration Identity

- Made the normalized service set part of the IaC Runner operation key and use the trigger-returned deployment ID for status polling; legacy env/ref-only polling fails closed when ambiguous.
- Split versioned, release-recomputable source identity from runtime/secret config identity and persist the exact checked-out SHA. Runtime-only config requires an explicit secret-free source builder.
- Config drift then proved source fingerprint provenance against its stored deploy ref and compared with the latest release; the 2026-08-17 hardening below supersedes that target with the explicit production marker. Pre-migration deployments remain `legacy_identity` without false drift.
- Proof: `uv run python -m pytest -q libs/tests` (`916 passed`); focused regression (`106 passed`); Ruff, compileall, workflow YAML parsing, and `git diff --check` passed.
- Rollout remains pending: after merge/release, each selected service backfills `IAC_SOURCE_CONFIG_HASH`/`IAC_DEPLOY_REF` on its next normal reconcile; legacy rows remain explicit and non-blocking, so no mass restart is required. The scheduled clean-checkout `--self-check` is the release proof. Local self-check is intentionally invalid while hash-input files differ from `HEAD`.

### 2026-07-17 GitHub Actions Node.js 24 Baseline

- Upgraded the release reconcile workflow from `checkout@v4`, `setup-python@v5`, and `upload-artifact@v4` to the repository's Node.js 24 majors (`v7`, `v6`, `v7`).
- Added a repository-wide workflow contract test so newly introduced workflows cannot bypass the minimum major baseline.
- Proof: full `libs/tests` (`953 passed`), focused workflow/reconcile contracts (`22 passed`), Ruff, workflow YAML parsing, SSOT/document governance, and `git diff --check` passed.

### 2026-08-17 Truthful Reconcile Hardening

- Kept Cloudflare observation failure distinct from an observed empty DNS zone.
- Made successful explicit prod promotion record an immutable `production/v*` marker; config drift now follows that marker, no-marker state fails closed, and later prod promotions cumulatively diff from the prior production marker rather than the prior staging candidate.
- Bound App production deploy requests to the marker's underlying infra release, while App staging continues to use the latest release candidate.
- Completed TrueAlpha data-engine's runtime/source identity split and added a generic contract over every runtime-only Deployer.
- Correlated Dokploy deploy errors with independent Docker health: discrepancies remain failed and delivered, but route to P2 reconciliation rather than an invented runtime outage.
- Fixed nested SSH health command quoting, combined TrueAlpha App readiness by boolean fields, tightened root-owned service-identity discovery, and aligned E2E assertions with public App/Shadow-DOM contracts.
- Rollout evidence: the latest full explicit production reconcile was `v1.1.48`; later
  `v1.1.49` single-service production runs left a mixed but explainable runtime, while
  `v1.1.50`-`v1.1.52` remained staging candidates. No `production/v*` marker exists yet.
  The first new-contract promotion must therefore explicitly use `before=v1.1.48`, target
  the reviewed release after staging soak, reconcile the cumulative delta, and only then
  record the marker. Until that high-risk step is approved and succeeds, config drift and
  App production requests intentionally fail closed as unknown desired state.
- Proof: full `libs/tests` (`1324 passed`), Ruff, changed-file Ruff format, compileall, workflow YAML parse, affected E2E collection, MkDocs build, harness manifest/status, and `git diff --check` passed.
