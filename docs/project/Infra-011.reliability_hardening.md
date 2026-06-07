# Infra-011: P1 Reliability Hardening

> **Status**: In Progress  
> **Issues**: #158, #162, #168, #182, #183  
> **Goal**: Make infra2 fail loudly for real deployment failures, core infra service outages, Vault Agent health drift, and stale or unverifiable backups.

## Situation

The out-of-band watchdog covers host reachability and alert bridge health, but
the P1 reliability review found four remaining hard gaps:

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

## Acceptance Criteria

| AC | Description | Proof |
|----|-------------|-------|
| Infra-011.1 | GitHub Actions deployment waits for the real IaC Runner sync result, fails on failed service syncs, and runs invoke without repo path shadowing Python stdlib modules. | `libs/tests/test_iac_runner_deploy_result.py`, `.github/workflows/deploy-platform.yml` |
| Infra-011.2 | P1 infra dependencies, authenticated 1Password Connect paths, IaC Runner process secrets, and generic Docker unhealthy/starting/restarting states have signal-owned watchdog coverage: internal probes run at minute-level cadence with consecutive-failure/recovery thresholds, Cloudflare checks public routes and probe heartbeats every 30 minutes, GitHub runs daily audit checks for Worker self-health and VPS macro health, public-route Cloudflare 1010 blocks are not classified as internal service outages, unchanged failures dedupe/renotify, recoveries notify once, and a consistency audit prevents unassigned signals, undocumented exclusions, stale monitors, and prod/staging drift. | `libs/tests/test_infra_probes.py`, `libs/tests/test_cloudflare_watchdog.py`, `libs/tests/test_bootstrap_health.py`, `libs/tests/test_vault_unsealer.py`, `libs/tests/test_iac_runner_deploy_result.py`, `libs/tests/test_out_of_band_watchdog.py`, `libs/tests/test_watchdog_consistency_audit.py`, `platform/12.alerting/compose.yaml`, `cloudflare/infra-watchdog/worker.js`, `docs/ssot/watchdog-signals.yaml`, `tools/watchdog_consistency_audit.py` |
| Infra-011.3 | Vault Agent Docker health checks token lookup, rendered-file presence, and unresolved template values, while mtime freshness remains an audit signal. | `libs/tests/test_vault_self_refresh_audit.py`, compose healthchecks |
| Infra-011.6 | IaC Runner sync ensures every runtime secret field consumed by custom service templates before deploy, creates missing Vault service paths when it has scoped write permission, and services without runtime secret templates explicitly opt out of the generic secret preflight. | `libs/tests/test_deployer.py`, `platform/*/deploy.py` |
| Infra-011.4 | Deployer-owned persistent data paths have backup inventory coverage, an archive/checksum runner, and manifest freshness verification. | `libs/tests/test_backup_verification.py`, `tools/backup_runner.py`, `docs/ssot/ops.backup-inventory.yaml` |
| Infra-011.5 | Public service routing ownership is single-source: compose-owned Traefik routers must not also use Dokploy domain generation. | `libs/tests/test_domain_routing_policy.py`, `docs/ssot/platform.domain.md` |
| Infra-011.7 | IaC Runner health checks include required runtime Python modules and binaries, missing dependency failures are classified, and optional audit inventory dependencies do not break invoke startup. | `libs/tests/test_iac_runner_deploy_result.py`, `bootstrap/06.iac_runner/webhook_server.py` |
| Infra-011.8 | Post-merge deployments externally rebuild IaC Runner through the VPS/Dokploy compose checkout before calling `/deploy` when runner bootstrap files change, disable Dokploy auto-deploy ownership for the runner, persist the target runner `GIT_SHA`, retry public runner health while Traefik/Cloudflare routing converges, and generic deployer hashes include local build/mount artifacts so code-backed infra services do not skip redeploys. | `libs/tests/test_iac_runner_deploy_result.py`, `libs/tests/test_deployer.py`, `.github/workflows/deploy-platform.yml`, `scripts/deploy_iac_runner_bootstrap.sh` |
| Infra-011.9 | Dokploy dynamic route canary fails fast by splitting compose upsert, deploy record creation, Docker container/Traefik label visibility, and public web/API route reachability before application PR previews depend on the platform; the workflow runs manually, hourly, and on canary implementation changes, publishes phase evidence in the GitHub step summary, and is also checked by the out-of-band watchdog so worker/deployment-record failures page before app CI burns time. | `libs/tests/test_dokploy_route_canary.py`, `libs/tests/test_out_of_band_watchdog.py`, `tools/dokploy_route_canary.py`, `tools/out_of_band_watchdog.py`, `.github/workflows/dokploy-route-canary.yml`, `.github/workflows/out-of-band-watchdog.yml` |
| Infra-011.10 | IaC Runner deploy control accepts only immutable commit SHAs, uses timestamped nonce signatures for CI deploy/status calls, redacts child stdout/stderr from public deploy responses, and prevents runner subprocesses from resolving Vault root tokens through 1Password. | `libs/tests/test_iac_runner_deploy_result.py`, `.github/workflows/deploy-platform.yml`, `bootstrap/06.iac_runner/webhook_server.py`, `bootstrap/06.iac_runner/sync_runner.py` |
| Infra-011.11 | Generic Dokploy deployer sync treats deployment records as the runtime apply proof: `compose.deploy` must produce a new running/done deployment record, retries once with `compose.redeploy` on no-op deploys, and fails fast instead of reporting success when both attempts leave runtime stale. | `libs/tests/test_deployer.py`, `libs/deployer.py` |

## Validation

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

Full validation before PR:

```bash
uv run ruff check .
uv run python -P -m pytest libs/tests -q
```
