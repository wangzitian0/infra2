# Infra-018: Repository Boundary Decoupling

**Status**: Archived
**Owner**: Infra
**Priority**: P1

## Goal

Make infra2 the unified workspace and deployment control repository while all runtime code
dependencies flow through a versioned SDK instead of recursive application-to-infra submodules.

## Result

TrueAlpha and Finance Report removed their infra2 `repo/` pins and consume the released
`infra2-sdk` contract independently. Finance Report requests deployments through the fail-closed
infra2 receiver. Infra2 pins the SDK and both applications under `repos/` as a development-only
workspace, while production identity remains independent infra tags, SDK SemVer, and App images.
Infra2 consumers import SDK contracts directly; the temporary local re-export modules are retired.

## Scope

- [x] Publish `infra2-sdk` v0.1.0 with side-effect-free shared contracts.
- [x] Pin infra2 to the released SDK and add the infra-owned App deploy receiver.
- [x] Migrate TrueAlpha away from its `repo/` submodule.
- [x] Migrate Finance Report deployment, tests, and docs away from its `repo/` submodule.
- [x] Add SDK and application repositories under infra2 `repos/` as workspace-only submodules.
- [x] Remove local SDK compatibility imports after all consumers import `infra2_sdk` directly.

## PR Links

- infra2-sdk: https://github.com/wangzitian0/infra2-sdk/pull/1
- infra2 SDK adoption and receiver: https://github.com/wangzitian0/infra2/pull/488
- infra2 Production evidence: https://github.com/wangzitian0/infra2/pull/489
- infra2 unified workspace: https://github.com/wangzitian0/infra2/pull/490
- TrueAlpha boundary migration: https://github.com/wangzitian0/truealpha/pull/217
- Finance Report SDK request preparation: https://github.com/wangzitian0/finance_report/pull/1878
- Finance Report receiver cutover: https://github.com/wangzitian0/finance_report/pull/1880

## Change Log

| Date | Change |
|---|---|
| 2026-07-15 | Published SDK v0.1.0 and adopted it in infra2. |
| 2026-07-15 | Added the fail-closed App deploy receiver and remote Production evidence verification. |
| 2026-07-15 | Removed the TrueAlpha and Finance Report reverse infra2 source dependencies. |
| 2026-07-15 | Added SDK, Finance Report, and TrueAlpha as workspace-only submodules under `repos/`. |
| 2026-07-15 | Migrated remaining infra consumers to direct SDK imports, retired compatibility modules, and archived the project. |

## Verification

- [x] `infra2-sdk` v0.1.0 release workflow and immutable artifacts succeeded.
- [x] Finance Report receiver staging run `29410732714` completed validation, canary, and deployment.
- [x] Production evidence verification bound source, staging, and merged-review facts to one release SHA without deploying Production.
- [x] Workspace gitlinks contain no recursive App-to-infra submodules.
- [x] MkDocs builds with the documented repository boundary.
- [x] Infra2 library tests pass after removing compatibility modules.

## TODOWRITE (Archived)

**Status**: Archived
**Owner**: Infra

### Completed Items

- [x] `truealpha/.gitmodules`: adopt the SDK and remove the `repo/` infra2 submodule.
- [x] `finance_report/.github/workflows/`: use the versioned App deploy request for staging, Production, and rollback.
- [x] `finance_report/tests/tooling/`: replace infra source assertions with SDK and wire-contract tests.
- [x] `finance_report/docs/`: replace `repo/docs` links with canonical infra URLs.
- [x] `infra2/repos/`: add SDK, Finance Report, and TrueAlpha as workspace-only submodules.
- [x] `.github/workflows/app-deploy-request.yml`: prove a controlled staging request.
- [x] `libs/app_deploy_request.py`: verify remote GitHub Production evidence fail-closed.
- [x] `libs/pipeline_stage_contract.py` and `libs/ci_gate_schema.py`: migrate consumers and remove compatibility modules.

## References

- [SSOT: core](../../ssot/core.md)
- [SSOT: ops.pipeline](../../ssot/ops.pipeline.md)
