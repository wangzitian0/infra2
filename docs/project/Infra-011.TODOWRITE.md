# Infra-011 TODOWRITE

**Project**: P1 Reliability Hardening  
**Status**: In Progress

## Acceptance Criteria

| AC | Description | Proof |
|----|-------------|-------|
| Infra-011.1 | Deploy workflow reports the real IaC Runner sync result instead of async acceptance. | `libs/tests/test_iac_runner_deploy_result.py` |
| Infra-011.2 | Infra service probes and out-of-band Docker health checks stay quiet on success and alert on unhealthy/starting/restarting containers. | `libs/tests/test_infra_probes.py`, `libs/tests/test_out_of_band_watchdog.py` |
| Infra-011.3 | Vault Agent Docker health avoids mtime false-unhealthy, rejects unresolved template values, and audit keeps stale-file detection. | `libs/tests/test_vault_self_refresh_audit.py` |
| Infra-011.4 | Backup inventory, archive/checksum generation, and freshness manifest verification are code-enforced. | `libs/tests/test_backup_verification.py` |
| Infra-011.5 | Compose-owned Traefik routing is not mixed with Dokploy-generated domain routing. | `libs/tests/test_domain_routing_policy.py` |
| Infra-011.6 | IaC Runner sync ensures every runtime secret field consumed by custom service templates before deploy. | `libs/tests/test_deployer.py` |
| Infra-011.7 | 1Password Connect bootstrap uses the canonical `infra2.0` credentials/token pair, stable `credential` field lookup, and bearer-auth initialization before health probes. | `libs/tests/test_bootstrap_health.py`, `libs/tests/test_vault_unsealer.py` |

## Issue Mapping

- #182: deploy result correctness.
- #183: live infra service probes.
- #168: Vault Agent rendered-file health contract.
- #158: off-host backup inventory and restore proof path.
- #162: external/synthetic and backup freshness alert coverage.
- #186: IaC Runner route ownership drift blocked main deploy health checks.
- #187: IaC Runner deploy failed after health recovery because repo `platform/` shadowed Python stdlib `platform`.
- #189: IaC Runner deploy sync lacks Vault automation token after stdlib shadow fix.
- #191: IaC Runner sync should use its scoped Vault app token.

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
- [x] Run full lint/test suite.
- [x] Open PR.
