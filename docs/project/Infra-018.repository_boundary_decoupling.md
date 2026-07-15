# Infra-018: Repository Boundary Decoupling

**Status**: In Progress
**Owner**: Infra
**Priority**: P1
**Branch**: `agent/app-deploy-production-evidence`

## Goal

Make infra2 the unified workspace and deployment control repository while all runtime code
dependencies flow through a versioned SDK instead of recursive application-to-infra submodules.

## Context

TrueAlpha has removed its infra2 `repo/` pin. Finance Report still executes infra2 deployment
source and reads infra-owned tests/docs from its `repo/` checkout. This prevents independent
upgrades and would create a recursive graph if infra2 added Finance Report as a workspace
submodule before the remaining edge is removed.

## Scope

- [x] Publish `infra2-sdk` v0.1.0 with side-effect-free shared contracts.
- [x] Pin infra2 to the released SDK and retain compatibility imports.
- [x] Add a fail-closed, infra-owned App deploy request receiver.
- [x] Migrate TrueAlpha away from its `repo/` submodule.
- [ ] Migrate Finance Report deployment and source-reading checks away from `repo/`.
- [ ] Add SDK and application repositories under infra2 `repos/` as workspace-only submodules.

## Deliverables

- A versioned SDK contract consumed independently by infra and applications.
- An App event boundary that keeps Dokploy/Vault authority inside infra2.
- One recursive-clone entry point without cyclic production dependencies.

## PR Links

- infra2-sdk: https://github.com/wangzitian0/infra2-sdk/pull/1
- infra2: https://github.com/wangzitian0/infra2/pull/488
- TrueAlpha: https://github.com/wangzitian0/truealpha/pull/217
- Finance Report SDK request preparation: https://github.com/wangzitian0/finance_report/pull/1878

## Change Log

| Date | Change |
|---|---|
| 2026-07-15 | Published SDK v0.1.0 and initialized the infra compatibility/receiver phase. |
| 2026-07-15 | Adopted SDK contracts and added a preview/staging receiver; cross-repo production remains deny-all pending remote evidence verification. |
| 2026-07-15 | Removed TrueAlpha's reverse infra2 dependency in truealpha#217. |
| 2026-07-15 | Proved the staging event boundary and added fail-closed GitHub verification for Production evidence. |

## Verification

- [x] `infra2-sdk` v0.1.0 Release workflow and artifacts succeed.
- [x] `uv run pytest libs/tests/test_sdk_contract_adoption.py`
- [x] `uv run pytest libs/tests/test_app_deploy_request.py`
- [x] `uv run pytest libs/tests/test_app_deploy_request_workflow.py`
- [x] Existing `libs/tests` pass without application migration (`907 passed`; coverage `72.91% >= 70.55%` baseline).
- [x] Finance Report source run [29392707543](https://github.com/wangzitian0/finance_report/actions/runs/29392707543) dispatched `v0.1.40` (`1d5daa713c657c0edf8179ac3147803300a5ba10`) and infra2 receiver run [29410732714](https://github.com/wangzitian0/infra2/actions/runs/29410732714) completed validation, canary, and staging deployment.
- [x] A side-effect-free Production plan verified source run `29392707543`, staging run `29392739223`, and merged Finance Report PR `#1862` at the same release SHA.
- [x] No Production deployment was executed while proving the receiver boundary.

## References

- [SSOT: core](../ssot/core.md)
- [SSOT: ops.pipeline](../ssot/ops.pipeline.md)
