# Infra-018: TODOWRITE (Repository Boundary Decoupling)

**Status**: Active
**Owner**: Infra

## Purpose

Track residual work required to remove recursive source dependencies without changing live
deployment authority or weakening production gates.

## Top Issues (Top 30)

- [x] `truealpha/.gitmodules`: adopt SDK where needed and remove the `repo/` infra2 submodule.
- [ ] `finance_report/.github/workflows/`: replace direct `repo/tools/deploy_v2.py` execution with the versioned App deploy request.
- [ ] `finance_report/tests/tooling/`: replace infra source-text assertions with SDK/wire-contract tests.
- [ ] `finance_report/docs/`: replace `repo/docs` relative links with canonical infra URLs.
- [ ] `infra2/repos/`: add SDK, Finance Report, and TrueAlpha as workspace-only submodules after both App edges are removed.
- [x] `.github/workflows/app-deploy-request.yml`: controlled Finance Report `v0.1.40` staging request succeeded in receiver run [29410732714](https://github.com/wangzitian0/infra2/actions/runs/29410732714).
- [x] `libs/app_deploy_request.py`: verify referenced GitHub run conclusions/SHAs and merged PR state before enabling production App requests.
- [ ] `libs/pipeline_stage_contract.py` and `libs/ci_gate_schema.py`: remove compatibility imports after all consumers use `infra2_sdk` directly.
