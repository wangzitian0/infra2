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

## Issue Mapping

- #182: deploy result correctness.
- #183: live infra service probes.
- #168: Vault Agent rendered-file health contract.
- #158: off-host backup inventory and restore proof path.
- #162: external/synthetic and backup freshness alert coverage.

## TODO

- [x] Create missing GitHub issues and claim existing P1 issues.
- [x] Add deploy result tests and synchronous `/deploy` behavior.
- [x] Add infra probe runner tests and compose service.
- [x] Update Vault Agent Docker health contract and tests.
- [x] Add backup inventory, runner, and manifest verifier tests.
- [ ] Run full lint/test suite.
- [ ] Open PR.
