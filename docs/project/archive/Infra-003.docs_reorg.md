# Infra-003: Documentation Reorganization

**Status**: Archived  
**Owner**: Infra  
**Priority**: P1

## Goal
Consolidate documentation into a clean, navigable structure with a stable SSOT and MkDocs site.

## Context
Docs are scattered across multiple paths and contain stale links/anchors. We need a single, consistent entry point and a clean docs site build.

## Scope
- Normalize docs structure and project catalog.
- Fix broken links/anchors across markdown files.
- Maintain MkDocs site config and Pages workflow.

## Deliverables
- Clean docs navigation and project index.
- Reduced doc lint/link warnings.
- MkDocs site builds without warnings.

## PR Links
- https://github.com/wangzitian0/infra2/pull/10

## Change Log
- 2025-12-28: Initialized docs reorg and Pages site.
- 2025-12-28: Fixed Top30 doc issues and normalized links/anchors.
- 2025-12-29: Aligned SSOT/onboarding/platform docs with Python + Dokploy deployment.
- 2025-12-29: Refactored E2E regression tests to match Dokploy/Vault/Authentik stack.
- 2025-12-29: Aligned env rules (docs + tooling) with new three-tier scheme.
- 2025-12-30: Fixed MkDocs external link warnings and aligned docs entry links.
- 2025-12-30: Updated root Quick Start to match 1Password bootstrap flow.
- 2025-12-30: Aligned bootstrap env seed notes with local.bootstrap behavior.
- 2025-12-30: Clarified E2E env example path and 1Password vault name.
- 2025-12-30: Simplified MkDocs nav and aligned onboarding/recovery/E2E guidance.
- 2025-12-30: Clarified docs site homepage vs repo README.
- 2025-12-30: Addressed review feedback on placeholders and E2E command clarity.
- 2025-12-30: Fixed init/env_vars SSOT alignment in EnvManager + E2E fallback; refreshed docs.
- 2025-12-30: Standardized E2E domain config on INTERNAL_DOMAIN (removed BASE_DOMAIN/E2E_DOMAIN).
- 2025-12-30: Archived Infra-003 after MkDocs build verification.
- 2025-12-30: Merged TODOWRITE into archived project record.

## Verification
- 2025-12-30: `mkdocs build --config-file docs/mkdocs.yml` (no warnings).

## TODOWRITE (Archived)

**Status**: Archived  
**Owner**: Infra

### Purpose
Track top documentation issues discovered across all markdown files.
Project closed on 2025-12-30; list retained for record.

### Top Issues (Top 30)
- [x] `docs/README.md`: 外链指向 `tools/README.md`，本仓库不存在 `tools/` 目录（已改为文本说明）。
- [x] `docs/README.md`: 外链指向 `envs/README.md`，本仓库不存在 `envs/` 目录（已改为文本说明）。
- [x] `docs/README.md`: 外链指向 `apps/README.md`，本仓库不存在 `apps/` 目录（已改为文本说明）。
- [x] `docs/README_tempate.md`: 模板内的占位链接不存在（已改为纯文本路径示例）。
- [x] `bootstrap/README.md`: 目录索引包含 `./06.casdoor/`（已改为非链接说明）。
- [x] `e2e_regressions/tests/bootstrap/README.md`: 指向缺失 SSOT（已改为 `core.md` / `ops.storage.md`）。
- [x] `e2e_regressions/tests/bootstrap/README.md`: 指向缺失 SSOT（已改为 `core.md` / `ops.storage.md`）。
- [x] `e2e_regressions/tests/bootstrap/README.md`: 指向缺失 SSOT（已改为 `core.md` / `ops.storage.md`）。
- [x] `e2e_regressions/tests/bootstrap/compute/README.md`: 指向缺失 SSOT（已改为 `core.md`）。
- [x] `e2e_regressions/tests/bootstrap/storage_layer/README.md`: 指向缺失 SSOT（已改为 `ops.storage.md`）。
- [x] `e2e_regressions/tests/bootstrap/network_layer/README.md`: 指向缺失 SSOT（已改为 `core.md`）。
- [x] `docs/onboarding/03.database.md`: 文中路径 `2.platform/` 已改为 `platform/`。
- [x] `docs/ssot/db.vault-integration.md`: 文中路径 `2.platform/` 已改为 `platform/`。
- [x] `docs/onboarding/01.quick-start.md`: “待部署”状态已改为“若未启用则不可用”。
- [x] `docs/onboarding/01.quick-start.md`: “待部署”状态已改为“若未启用则不可用”。
- [x] `docs/onboarding/06.observability.md`: “待部署”状态已改为“若未启用则不可用”。
- [x] `docs/onboarding/06.observability.md`: “待部署”状态已改为“若未启用则不可用”。
- [x] `docs/ssot/bootstrap.nodep.md`: 版本表补充为 unknown，并注明需补齐。
- [x] `docs/ssot/bootstrap.nodep.md`: 安装日期补充为 unknown，并注明需补齐。
- [x] `docs/ssot/bootstrap.nodep.md`: 验证状态改为“未验证”明确状态。
- [x] `docs/ssot/core.md`: Planned 测试改为 Backlog 表述。
- [x] `docs/ssot/ops.alerting.md`: Planned 测试改为 Backlog 表述。
- [x] `docs/ssot/ops.observability.md`: Planned 测试改为 Backlog 表述。
- [x] `docs/ssot/ops.recovery.md`: Planned 测试改为 Backlog 表述。
- [x] `docs/ssot/platform.ai.md`: Pending + TODO 改为 Backlog 说明。
- [x] `docs/project/archive/Infra-003.docs_reorg.md`: PR Links 更新为 PR #10。
- [x] `docs/project/Infra-004.authentik_install.md`: PR Links 改为 “None yet.”。
- [x] `docs/project/Infra-004.authentik_install.md`: Change Log 改为 “None yet.”。
- [x] `docs/ssot/platform.sso.md`: 由 Casdoor 迁移到 Authentik，修正文档真理来源。
- [x] `docs/ssot/platform.secrets.md`: 内容合并到 `bootstrap.vars_and_secrets.md` 并移除。
- [x] `docs/ssot/**`: 清理 Terraform/Kubernetes 术语并对齐 Dokploy + Compose 实际实现。
- [x] `docs/onboarding/**`: 重写为 Dokploy + Authentik 实际流程。
- [x] `platform/*/README.md`: 更新为 Invoke 任务说明。
- [x] `docs/ssot/ops.standards.md`: 改为 Dokploy/Compose 规范。
- [x] `docs/ssot/ops.recovery.md`: 改为 Docker/VPS 侧恢复流程。
- [x] `e2e_regressions/**`: 移除 Terraform/K8s/Casdoor/Kubero 依赖，改为 Dokploy/Vault/Authentik。
- [x] `e2e_regressions/.env.example`: 更新为新环境变量清单。
- [x] `docs/project/Infra-004.authentik_install.md`: 修复 MkDocs 外链警告（指向 libs/platform README）。
- [x] `docs/ssot/bootstrap.vars_and_secrets.md`: 修复 MkDocs 外链警告（指向 bootstrap README）。
- [x] `docs/README.md`: Tools 入口恢复链接并更新日期。
- [x] `README.md`: 环境变量 SSOT 链接与 tools 说明对齐代码。
- [x] `docs/project/README.md`: Infra-004 状态与项目文件对齐为 In Progress。
- [x] `README.md`: Quick Start 与 1Password/bootstrap 流程对齐，移除不存在的 `.env.example` 步骤。
- [x] `docs/ssot/bootstrap.vars_and_secrets.md`: 修正 local.bootstrap 不生成 `.env` 的说明。
- [x] `docs/ssot/core.md`: 环境变量文件位置与 bootstrap 现状对齐。
- [x] `docs/ssot/ops.e2e-regressions.md`: 明确 `.env.example` 的目录路径。
- [x] `README.md`: 1Password Vault 名称大小写对齐为 Infra2。
- [x] `docs/mkdocs.yml`: 首页与导航标注规划中内容，移除模板页面展示。
- [x] `docs/ssot/ops.pipeline.md`: 文档输入源与 mkdocs 配置对齐。
- [x] `docs/onboarding/01.quick-start.md`: 补充访问凭证获取与权限提示。
- [x] `docs/onboarding/03.database.md`: 补充 dokploy-network 示例与 Vault 登录步骤。
- [x] `docs/ssot/db.vault-integration.md`: 移除非敏感值写入 Vault 的示例。
- [x] `docs/ssot/ops.recovery.md`: 1Password 路径与域名占位符对齐 SSOT。
- [x] `docs/ssot/ops.e2e-regressions.md`: 测试命令与 e2e_regressions/README 对齐。
- [x] `docs/README.md`: 说明文档站首页与仓库 README 的职责区分。
- [x] `docs/onboarding/03.database.md`: 占位符格式统一并说明 VAULT_TOKEN 来源。
- [x] `docs/onboarding/04.secrets.md`: 权限提示用语对齐。
- [x] `e2e_regressions/README.md`: 测试命令位置说明更清晰（去掉 cd ..）。
- [x] `docs/ssot/ops.recovery.md`: 统一占位符格式。
- [x] `libs/README.md` + `tools/README.md`: 增补 libs/tools 总览与 env/local CLI 一致性说明。
- [x] `tools/loader.py` + `bootstrap/05.vault/*` + `platform/01.postgres/shared_tasks.py`: CLI 日志统一改为 `libs.console` 输出。
- [x] `tools/local_init.py`: CLI 安装/提示输出统一改为 `libs.console` 风格。
- [x] `README.md` + `docs/README.md` + `docs/onboarding/README.md` + `docs/ssot/README.md`: 统一命令示例为 `invoke` 并提示 `uv run invoke` 备用。
- [x] `docs/ssot/bootstrap.vars_and_secrets.md` + `README.md`: env CLI 示例补齐 `--service` 与说明。
- [x] `libs/deployer.py` + `bootstrap/04.1password/tasks.py` + `bootstrap/05.vault/tasks.py` + `platform/10.authentik/deploy.py`: CLI 日志文案统一为 pre/post-compose。
- [x] `docs/ssot/platform.automation.md` + `docs/ssot/platform.sso.md` + `platform/*/README.md`: 统一 pre/post-compose 命名展示。
