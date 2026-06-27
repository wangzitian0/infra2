# Infra-011: Reliability and CI/CD Stage Contract

> **Status**: In Progress  
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
| Infra-011.4 | Deployer-owned persistent data paths have backup inventory coverage, an archive/checksum runner, and manifest freshness verification. | `libs/tests/test_backup_verification.py`, `tools/backup_runner.py`, `docs/ssot/ops.backup-inventory.yaml` |
| Infra-011.5 | Public service routing ownership is single-source: compose-owned Traefik routers must not also use Dokploy domain generation. | `libs/tests/test_domain_routing_policy.py`, `docs/ssot/platform.domain.md` |
| Infra-011.7 | IaC Runner health checks include required runtime Python modules and binaries, missing dependency failures are classified, and optional audit inventory dependencies do not break invoke startup. | `libs/tests/test_iac_runner_deploy_result.py`, `bootstrap/06.iac_runner/webhook_server.py` |
| Infra-011.8 | Post-merge deployments externally rebuild IaC Runner through the VPS/Dokploy compose checkout before calling `/deploy` when runner bootstrap files change, disable Dokploy auto-deploy ownership for the runner, persist the target runner `GIT_SHA`, retry public runner health while Traefik/Cloudflare routing converges, and generic deployer hashes include local build/mount artifacts so code-backed infra services do not skip redeploys. | `libs/tests/test_iac_runner_deploy_result.py`, `libs/tests/test_deployer.py`, `.github/workflows/deploy.yml`, `scripts/deploy_iac_runner_bootstrap.sh` |
| Infra-011.9 | Dokploy dynamic route canary fails fast by splitting canary configuration, compose upsert, compose source-type drift, deploy record creation, Docker container/exact Traefik label visibility, and public web/API route reachability before application PR previews depend on the platform; the workflow runs manually, hourly, and on canary implementation changes, uses stable default host/compose pairs so one run cannot inherit another run's stale labels, injects a non-sensitive deploy nonce so every run still requires a fresh deployment record, reads deployment records from Dokploy's compose deployment listing API before falling back to embedded compose snapshots, can explicitly delete/recreate only guarded `route-canary*` / `dokploy-route-canary*` test assets after deploy and redeploy both no-op, normalizes recreated canary composes back to `sourceType=raw`, fails closed when required environment configuration is absent, turns public route read timeouts into route evidence rather than uncaught exceptions, and tags out-of-band failures with structured domains so configuration, control-plane, route, heartbeat, SSH, and bridge failures are distinguishable in alerts; it also publishes phase evidence including compose source/status and latest deployment log path in the GitHub step summary, and is checked by the out-of-band watchdog so worker/deployment-record failures page before app CI burns time. | `libs/tests/test_dokploy_route_canary.py`, `libs/tests/test_out_of_band_watchdog.py`, `tools/dokploy_route_canary.py`, `tools/out_of_band_watchdog.py`, `.github/workflows/ops-checks.yml` |
| Infra-011.10 | IaC Runner deploy control accepts only immutable commit SHAs, uses timestamped nonce signatures for CI deploy/status calls, redacts child stdout/stderr from public deploy responses, and prevents runner subprocesses from resolving Vault root tokens through 1Password. | `libs/tests/test_iac_runner_deploy_result.py`, `.github/workflows/deploy.yml`, `bootstrap/06.iac_runner/webhook_server.py`, `bootstrap/06.iac_runner/sync_runner.py` |
| Infra-011.11 | Generic Dokploy deployer sync treats deployment records as the runtime apply proof: `compose.deploy` must produce a new running/done deployment record from Dokploy's compose deployment listing API, retries once with `compose.redeploy` on no-op deploys, and fails fast instead of reporting success when both attempts leave runtime stale. | `libs/tests/test_deployer.py`, `libs/deploy/deployer.py` |
| Infra-011.12 | CI/CD, watchdog, canary, and probe outputs separate environment from pipeline stage and share one sparse Env x Stage result schema with explicit stage names, failure domains, duration, deadline, external dependency flag, and suppression reason. | `docs/ssot/ops.pipeline.md`, `docs/ssot/ops.alerting.md`, `libs/tests/test_pipeline_stage_contract.py` |
| Infra-011.13 | External dependencies fail in preflight before expensive stages: GitHub secrets/vars, IaC Runner health dependencies, Cloudflare KV/secrets/config JSON, Dokploy API credentials/environment IDs, SSH diagnostics config, and Feishu delivery mode configuration are all classified as `configuration` or `external-dependency` failures. | `libs/tests/test_pipeline_stage_contract.py`, `libs/tests/test_cloudflare_watchdog.py`, `libs/tests/test_out_of_band_watchdog.py`, `libs/tests/test_dokploy_route_canary.py`, `.github/workflows/deploy.yml` |
| Infra-011.14 | Long-running CI/CD stages publish budget evidence: soft budget, hard deadline, elapsed duration, current stage age for in-progress deploys, and budget breach classification without exposing child stdout/stderr. | `libs/tests/test_iac_runner_deploy_result.py`, `libs/tests/test_pipeline_stage_contract.py`, `.github/workflows/deploy.yml`, `bootstrap/06.iac_runner/webhook_server.py`, `bootstrap/06.iac_runner/sync_runner.py` |
| Infra-011.15 | Cross-stage disagreements are defined and measurable: internal service healthy plus public route failed, heartbeat fresh plus probe group failed, heartbeat stale plus route healthy, canary route failed plus app readiness pending, and GitHub fallback host failure plus Cloudflare route pass all produce deterministic disagreement records. | `libs/tests/test_pipeline_stage_contract.py`, `libs/tests/test_infra_probes.py`, `libs/tests/test_cloudflare_watchdog.py`, `libs/tests/test_dokploy_route_canary.py` |
| Infra-011.16 | Acceleration decisions are evidence-gated through the Env x Stage matrix: selected `iac_pinned` services may reconcile automatically from reviewed infra2 `main` only when fan-out evidence identifies changed inputs and the Deployer config-hash gate proves no-op vs restart; app production remains manual and environment-protected. The "from reviewed main" precondition is **fail-closed enforced**: before any apply, `assert_after_on_main` resolves the promoted tag and refuses it unless reachable from `origin/main`, so a release tag cut on an unmerged/off-main feature branch cannot drive a real staging/prod deploy (the v1.1.16 incident); `--dry-run` plans are exempt. | `docs/ssot/ops.pipeline.md`, `tools/reconcile_iac_inputs.py`, `libs/tests/test_iac_runner_deploy_result.py`, `libs/tests/test_reconcile_iac_inputs.py`, `.github/workflows/reconcile-iac-inputs.yml` |
| Infra-011.17 | Off-host backup durability is rehearsed by restoring the latest verified artifact into an explicitly throwaway target and checking database invariants; live-looking production containers are refused by default. | `libs/tests/test_backup_verification.py`, `tools/backup_restore_rehearsal.py`, `docs/ssot/ops.recovery.md` |

## Counterfactual Requirements

| Counterfactual | Required Result |
|----------------|-----------------|
| Cloudflare Worker has invalid `WATCHDOG_TARGETS_JSON`. | A `config-preflight` stage failure is recorded and tested; it must not masquerade as a public route outage. |
| `WATCHDOG_STATE` KV binding is missing. | Heartbeat and dedupe state failures are classified as `configuration`, with a deterministic failure domain. |
| Feishu app bot secret is missing. | Notification delivery preflight fails before route failures are deduped as sent. |
| IaC Runner `/health` returns degraded because a Python module or binary is missing. | `deploy.yml` bootstrap fails during `iac-health-preflight`, not after starting `/deploy`. |
| `/deploy/status` remains `in_progress` beyond budget. | GitHub summary reports the current deployment stage age and hard-timeout breach. |
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
  libs/tests/test_dokploy_route_canary.py \
  libs/tests/test_pipeline_stage_contract.py \
  -q
```

Full validation before PR:

```bash
uv run ruff check .
uv run python -P -m pytest libs/tests -q
```
