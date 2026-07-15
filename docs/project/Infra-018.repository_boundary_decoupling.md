# Infra-018: Repository Boundary Decoupling

**Status**: In Progress
**Owner**: Infra
**Priority**: P1
**Branch**: `agent/sdk-deploy-receiver`

## Goal

Make infra2 the unified workspace and deployment control repository while all runtime code
dependencies flow through a versioned SDK instead of recursive application-to-infra submodules.

## Context

Finance Report and TrueAlpha currently pin infra2 as `repo/`. Finance Report also executes
infra2 deployment source and reads infra-owned tests/docs from that checkout. This prevents
independent upgrades and would create a recursive graph if infra2 added the applications as
workspace submodules first.

## Scope

- [x] Publish `infra2-sdk` v0.1.0 with side-effect-free shared contracts.
- [x] Pin infra2 to the released SDK and retain compatibility imports.
- [x] Add a fail-closed, infra-owned App deploy request receiver.
- [ ] Migrate TrueAlpha away from its `repo/` submodule.
- [ ] Migrate Finance Report deployment and source-reading checks away from `repo/`.
- [ ] Add SDK and application repositories under infra2 `repos/` as workspace-only submodules.

## Deliverables

- A versioned SDK contract consumed independently by infra and applications.
- An App event boundary that keeps Dokploy/Vault authority inside infra2.
- One recursive-clone entry point without cyclic production dependencies.

## PR Links

- infra2-sdk: https://github.com/wangzitian0/infra2-sdk/pull/1
- infra2: None yet.

## Change Log

| Date | Change |
|---|---|
| 2026-07-15 | Published SDK v0.1.0 and initialized the infra compatibility/receiver phase. |
| 2026-07-15 | Adopted SDK contracts and added a preview/staging receiver; cross-repo production remains deny-all pending remote evidence verification. |

## Verification

- [x] `infra2-sdk` v0.1.0 Release workflow and artifacts succeed.
- [x] `uv run pytest libs/tests/test_sdk_contract_adoption.py`
- [x] `uv run pytest libs/tests/test_app_deploy_request.py`
- [x] `uv run pytest libs/tests/test_app_deploy_request_workflow.py`
- [x] Existing `libs/tests` pass without application migration (`892 passed`; coverage `72.74% >= 70.55%` baseline).
- [ ] Staging deployment through the receiver succeeds before Finance Report removes `repo/`.

## References

- [SSOT: core](../ssot/core.md)
- [SSOT: ops.pipeline](../ssot/ops.pipeline.md)
