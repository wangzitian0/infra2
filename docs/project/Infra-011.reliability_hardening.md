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
- Backup coverage was not code-enforced against deployer-owned `DATA_PATH`
  services.

## Acceptance Criteria

| AC | Description | Proof |
|----|-------------|-------|
| Infra-011.1 | GitHub Actions deployment waits for the real IaC Runner sync result and fails on failed service syncs. | `libs/tests/test_iac_runner_deploy_result.py`, `.github/workflows/deploy-platform.yml` |
| Infra-011.2 | P1 infra dependencies have code-owned probes that alert through `platform/12.alerting`. | `libs/tests/test_infra_probes.py`, `platform/12.alerting/compose.yaml` |
| Infra-011.3 | Vault Agent Docker health checks token lookup and rendered-file presence, while mtime freshness remains an audit signal. | `libs/tests/test_vault_self_refresh_audit.py`, compose healthchecks |
| Infra-011.4 | Deployer-owned persistent data paths have backup inventory coverage, an archive/checksum runner, and manifest freshness verification. | `libs/tests/test_backup_verification.py`, `tools/backup_runner.py`, `docs/ssot/ops.backup-inventory.yaml` |

## Validation

```bash
uv run python -P -m pytest \
  libs/tests/test_iac_runner_deploy_result.py \
  libs/tests/test_infra_probes.py \
  libs/tests/test_backup_verification.py \
  libs/tests/test_vault_self_refresh_audit.py \
  -q
```

Full validation before PR:

```bash
uv run ruff check .
uv run python -P -m pytest libs/tests -q
```
