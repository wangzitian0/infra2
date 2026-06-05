# Infra-011 TODOWRITE

**Project**: P1 Reliability Hardening  
**Status**: In Progress

## Acceptance Criteria

| AC | Description | Proof |
|----|-------------|-------|
| Infra-011.1 | Deploy workflow reports the real IaC Runner sync result instead of async acceptance. | `libs/tests/test_iac_runner_deploy_result.py` |
| Infra-011.2 | Infra service probes run through the alert bridge and stay quiet on success. | `libs/tests/test_infra_probes.py` |
| Infra-011.3 | Vault Agent Docker health avoids mtime false-unhealthy while audit keeps stale-file detection. | `libs/tests/test_vault_self_refresh_audit.py` |
| Infra-011.4 | Backup inventory, archive/checksum generation, and freshness manifest verification are code-enforced. | `libs/tests/test_backup_verification.py` |
| Infra-011.5 | Compose-owned Traefik routing is not mixed with Dokploy-generated domain routing. | `libs/tests/test_domain_routing_policy.py` |

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
- [x] Add backup inventory, runner, and manifest verifier tests.
- [x] Add routing ownership policy test and remove current mixed-mode offenders.
- [x] Run IaC Runner invoke tasks without `platform/` shadowing Python stdlib imports.
- [x] Use Dokploy Domains for simple IaC Runner and Wealthfolio public routes.
- [x] Resolve Vault root token inside IaC Runner sync tasks without putting it in GitHub Actions.
- [x] Prefer IaC Runner scoped Vault app token for sync secret reads.
- [x] Run full lint/test suite.
- [x] Open PR.
