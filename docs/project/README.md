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
- [Infra-019.TODOWRITE.md](./Infra-019.TODOWRITE.md) - workspace harness follow-ups

> Infra-001's TODOWRITE was merged into
> [its archived project file](./archive/Infra-001.bootstrap_setup.md) per the
> archive procedure in [todowrite_template.md](./todowrite_template.md); its
> only remaining entry recorded that the content had already moved to
> Infra-006.

## Related
- [README.md](../../README.md) - 工程入口
- [docs/onboarding/README.md](../onboarding/README.md) - Onboarding
- [docs/ssot/README.md](../ssot/README.md) - SSOT 索引
- [AGENTS.md](../../AGENTS.md) - AI 行为准则
- [docs/README.md](../README.md) - 文档索引

## Active Projects

<!-- BEGIN GENERATED ACTIVE PROJECTS (tools/gen_project_index.py) -->

- [Infra-022: Production Resilience & Disaster Recovery (生产韧性与容灾兜底)](./Infra-022.production_resilience_and_dr.md) - **In Progress**
- [Infra-020: TrueAlpha Production DataHub](./Infra-020.truealpha_production_datahub.md) - **In Progress**
- [Infra-019: Workspace Harness Control Plane](./Infra-019.harness_control_plane.md) - **In Progress**
- [Infra-017: TrueAlpha Dagster Capture Runtime](./Infra-017.truealpha_dagster_capture.md) - **Active — Production companion runtime to TrueAlpha**
- [Infra-013: Service Registry as Single Source of Truth](./Infra-013.service_registry_ssot.md) - **In Progress**
- [Infra-012: Watchdog Observability & Alert Completeness](./Infra-012.watchdog_observability.md) - **In Progress (Phase 1 delivered, Phase 2/3 actively closing)**
- [Infra-006: Documentation Engineering](./Infra-006.documentation_engineering.md) - **In Progress**

<!-- END GENERATED ACTIVE PROJECTS -->

## Archived Projects

<!-- BEGIN GENERATED ARCHIVED PROJECTS (tools/gen_project_index.py) -->

- [Infra-021: OpenPanel Installation](./archive/Infra-021.openpanel_install.md) - **Archived — Completed**
- [Infra-018: Repository Boundary Decoupling](./archive/Infra-018.repository_boundary_decoupling.md) - **Archived**
- [Infra-016: CI Gate Inventory — coordinate-ize infra CI, and de-overlap app vs infra responsibilities](./archive/Infra-016.ci_gate_inventory.md) - **Archived — Closed (#460 contracts + #461 infra inventory/audit completed and kept frozen; epic #459 closed as not planned — converged into boundary governance, finance_report#876; app-side phases 3–4 not pursued)**
- [Infra-015: deploy_v2 — the unified, trustworthy deploy front door](./archive/Infra-015.deploy_v2_front_door.md) - **Archived — implementation complete & live-verified; companion finance_report#1173**
- [Infra-014: finance_report Observability Wiring (OTel → SigNoz + OpenPanel per-env)](./archive/Infra-014.finance_report_observability.md) - **Archived — Completed**
- [Infra-011: Reliability and CI/CD Stage Contract](./archive/Infra-011.reliability_hardening.md) - **Archived — Completed (#158, #162, #168, #182, #183 all closed; AC table kept — tests cite `Infra-011.x` ids)**
- [Infra-010: IaC & Service Verification](./archive/Infra-010.iac_and_service_verification.md) - **Archived — Completed 2026-01-24**
- [Infra-009: Finance Report Deployment](./archive/Infra-009.finance_report_deploy.md) - **Archived — Completed (superseded by deploy_v2 Infra-015)**
- [Infra-008: Platform Multi-Environment (Staging)](./archive/Infra-008.platform_multi_env.md) - **Archived — Completed**
- [Infra-007: SigNoz Observability Platform Installation](./archive/Infra-007.signoz_install.md) - **Archived — Completed**
- [Infra-005: Homer Portal + SSO Protection](./archive/Infra-005.homer_portal.md) - **Archived — Completed (PR #28)**
- [Infra-004: Authentik Installation](./archive/Infra-004.authentik_install.md) - **Archived — Completed (PR #28)**
- [Infra-003: Documentation Reorganization](./archive/Infra-003.docs_reorg.md) - **Archived**
- [Infra-002: 1Password + Vault Installation](./archive/Infra-002.1password_vault_install.md) - **Archived**
- [Infra-001: Bootstrap Base Setup](./archive/Infra-001.bootstrap_setup.md) - **Archived**

<!-- END GENERATED ARCHIVED PROJECTS -->

---
*Last updated: 2026-09-15*
