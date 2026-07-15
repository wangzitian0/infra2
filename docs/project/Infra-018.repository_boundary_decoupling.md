# Infra-018: Repository Boundary Decoupling

**Status**: Implementation Complete
**Owner**: Infra
**Priority**: P1
**Branch**: `agent/unified-workspace-repos`

## Goal

Make infra2 the unified workspace and deployment control repository while all runtime code
dependencies flow through a versioned SDK instead of recursive application-to-infra submodules.

## Context

TrueAlpha and Finance Report have removed their infra2 `repo/` pins. Both applications consume
the released SDK contract independently, and Finance Report requests deployments through the
infra2 receiver. The reverse edges are gone, so infra2 can now pin all three repositories as a
workspace without creating a recursive production dependency.

## Scope

- [x] Publish `infra2-sdk` v0.1.0 with side-effect-free shared contracts.
- [x] Pin infra2 to the released SDK and retain compatibility imports.
- [x] Add a fail-closed, infra-owned App deploy request receiver.
- [x] Migrate TrueAlpha away from its `repo/` submodule.
- [x] Migrate Finance Report deployment and source-reading checks away from `repo/`.
- [x] Add SDK and application repositories under infra2 `repos/` as workspace-only submodules.

## Deliverables

- A versioned SDK contract consumed independently by infra and applications.
- An App event boundary that keeps Dokploy/Vault authority inside infra2.
- One recursive-clone entry point without cyclic production dependencies.

## PR Links

- infra2-sdk: https://github.com/wangzitian0/infra2-sdk/pull/1
- infra2: https://github.com/wangzitian0/infra2/pull/488
- infra2 Production evidence: https://github.com/wangzitian0/infra2/pull/489
- TrueAlpha: https://github.com/wangzitian0/truealpha/pull/217
- Finance Report SDK request preparation: https://github.com/wangzitian0/finance_report/pull/1878
- Finance Report receiver cutover: https://github.com/wangzitian0/finance_report/pull/1880

## Change Log

| Date | Change |
|---|---|
| 2026-07-15 | Published SDK v0.1.0 and initialized the infra compatibility/receiver phase. |
| 2026-07-15 | Adopted SDK contracts and added a preview/staging receiver; cross-repo production remains deny-all pending remote evidence verification. |
| 2026-07-15 | Removed TrueAlpha's reverse infra2 dependency in truealpha#217. |
| 2026-07-15 | Proved the staging event boundary and added fail-closed GitHub verification for Production evidence. |
| 2026-07-15 | Cut Finance Report staging/prod/rollback over to the receiver and removed its reverse infra2 source dependency. |
| 2026-07-15 | Added SDK, Finance Report, and TrueAlpha as workspace-only submodules under `repos/`. |

## Verification

- [x] `infra2-sdk` v0.1.0 Release workflow and artifacts succeed.
- [x] `uv run pytest libs/tests/test_sdk_contract_adoption.py`
- [x] `uv run pytest libs/tests/test_app_deploy_request.py`
- [x] `uv run pytest libs/tests/test_app_deploy_request_workflow.py`
- [x] Existing `libs/tests` pass without application migration (`907 passed`; coverage `72.91% >= 70.55%` baseline).
- [x] Finance Report source run [29392707543](https://github.com/wangzitian0/finance_report/actions/runs/29392707543) dispatched `v0.1.40` (`1d5daa713c657c0edf8179ac3147803300a5ba10`) and infra2 receiver run [29410732714](https://github.com/wangzitian0/infra2/actions/runs/29410732714) completed validation, canary, and staging deployment.
- [x] A side-effect-free Production plan verified source run `29392707543`, staging run `29392739223`, and merged Finance Report PR `#1862` at the same release SHA.
- [x] No Production deployment was executed while proving the receiver boundary.
- [x] Workspace gitlinks pin `infra2-sdk@bc83d5e`, `finance_report@0796944`, and `truealpha@a6a04ba` without recursive App-to-infra submodules.
- [x] Workspace isolation verification: MkDocs build succeeds and infra2 `libs/tests` pass (`908 passed`) with initialized submodules.

## References

- [SSOT: core](../ssot/core.md)
- [SSOT: ops.pipeline](../ssot/ops.pipeline.md)
