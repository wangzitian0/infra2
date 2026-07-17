# Project Portfolio

**SSOT Type**: Implementation status and portfolio index  
**Scope**: Infra project catalog and status tracking for Infra-xxx items.

## Structure
- Active projects live in `docs/project/`.
- Archived projects live in `docs/project/archive/`.
- Each project file includes PR links and an embedded change log section.
- Archived projects merge the project record + TODOWRITE into a single file.
- Docs reorg TODO record is archived in `docs/project/archive/Infra-003.docs_reorg.md`.

## Templates
- Project template: [project_template.md](./project_template.md)
- TODOWRITE template: [todowrite_template.md](./todowrite_template.md)

## Global TODOWRITE
- [Infra-001.TODOWRITE.md](./Infra-001.TODOWRITE.md) - Legacy/global issues
- [Infra-015.TODOWRITE.md](./Infra-015.TODOWRITE.md) - deploy_v2 front door residuals (sender #1173 + archive)
- [Infra-019.TODOWRITE.md](./Infra-019.TODOWRITE.md) - workspace harness follow-ups

## Related
- [README.md](../../README.md) - 工程入口
- [docs/onboarding/README.md](../onboarding/README.md) - Onboarding
- [docs/ssot/README.md](../ssot/README.md) - SSOT 索引
- [AGENTS.md](../../AGENTS.md) - AI 行为准则
- [docs/README.md](../README.md) - 文档索引

## Active Projects

<!-- BEGIN GENERATED ACTIVE PROJECTS (tools/gen_project_index.py) -->

- [Infra-021: OpenPanel Installation](./Infra-021.openpanel_install.md) - **In Progress**
- [Infra-020: TrueAlpha Production DataHub](./Infra-020.truealpha_production_datahub.md) - **In Progress**
- [Infra-019: Workspace Harness Control Plane](./Infra-019.harness_control_plane.md) - **In Progress**
- [Infra-017: TrueAlpha Dagster Capture Runtime](./Infra-017.truealpha_dagster_capture.md) - **In Progress**
- [Infra-016: CI Gate Inventory — coordinate-ize infra CI, and de-overlap app vs infra responsibilities](./Infra-016.ci_gate_inventory.md) - **In Progress**
- [Infra-015: deploy_v2 — the unified, trustworthy deploy front door](./Infra-015.deploy_v2_front_door.md) - **Implementation complete & live-verified; companion finance_report#1173 (app-repo sender) pending merge — archive (merge record + [TODOWRITE](./Infra-015.TODOWRITE.md)) once it lands**
- [Infra-014: finance_report Observability Wiring (OTel → SigNoz + OpenPanel per-env)](./Infra-014.finance_report_observability.md) - **In Progress**
- [Infra-013: Service Registry as Single Source of Truth](./Infra-013.service_registry_ssot.md) - **In Progress**
- [Infra-012: Watchdog Observability & Alert Completeness](./Infra-012.watchdog_observability.md) - **In Progress (Phase 1 delivered, Phase 2/3 actively closing)**
- [Infra-011: Reliability and CI/CD Stage Contract](./Infra-011.reliability_hardening.md) - **In Progress**
- [Infra-010: IaC & Service Verification](./Infra-010.iac_and_service_verification.md) - **Completed**
- [Infra-009: Finance Report Deployment](./Infra-009.finance_report_deploy.md) - **In Progress**
- [Infra-008: Platform Multi-Environment (Staging)](./Infra-008.platform_multi_env.md) - **In Progress**
- [Infra-007: SigNoz Observability Platform Installation](./Infra-007.signoz_install.md) - **In Progress**
- [Infra-006: Documentation Engineering](./Infra-006.documentation_engineering.md) - **In Progress**
- [Infra-005: Homer Portal + SSO Protection](./Infra-005.homer_portal.md) - **Completed**
- [Infra-004: Authentik Installation](./Infra-004.authentik_install.md) - **Completed ✅**

<!-- END GENERATED ACTIVE PROJECTS -->

## Archived Projects

<!-- BEGIN GENERATED ARCHIVED PROJECTS (tools/gen_project_index.py) -->

- [Infra-018: Repository Boundary Decoupling](./archive/Infra-018.repository_boundary_decoupling.md) - **Archived**
- [Infra-003: Documentation Reorganization](./archive/Infra-003.docs_reorg.md) - **Archived**
- [Infra-002: 1Password + Vault Installation](./archive/Infra-002.1password_vault_install.md) - **Archived**
- [Infra-001: Bootstrap Base Setup](./archive/Infra-001.bootstrap_setup.md) - **Archived**

<!-- END GENERATED ARCHIVED PROJECTS -->

---
*Last updated: 2026-07-18*
