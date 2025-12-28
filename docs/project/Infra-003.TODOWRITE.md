# Infra-003: TODOWRITE (Docs Reorg)

**Status**: Active  
**Owner**: Infra

## Purpose
Track top documentation issues discovered across all markdown files.

## Top Issues (Top 30)
1. `docs/README.md`: 外链指向 `tools/README.md`，本仓库不存在 `tools/` 目录。
2. `docs/README.md`: 外链指向 `envs/README.md`，本仓库不存在 `envs/` 目录。
3. `docs/README.md`: 外链指向 `apps/README.md`，本仓库不存在 `apps/` 目录。
4. `docs/README_tempate.md`: 模板内的 `../docs/ssot/xxx.md` 占位链接不存在。
5. `bootstrap/README.md`: 目录索引包含 `./06.casdoor/`，实际目录缺失。
6. `e2e_regressions/tests/bootstrap/README.md`: 引用 `docs/ssot/bootstrap.compute.md`（缺失）。
7. `e2e_regressions/tests/bootstrap/README.md`: 引用 `docs/ssot/bootstrap.storage.md`（缺失）。
8. `e2e_regressions/tests/bootstrap/README.md`: 引用 `docs/ssot/bootstrap.network.md`（缺失）。
9. `e2e_regressions/tests/bootstrap/compute/README.md`: 引用 `docs/ssot/bootstrap.compute.md`（缺失）。
10. `e2e_regressions/tests/bootstrap/storage_layer/README.md`: 引用 `docs/ssot/bootstrap.storage.md`（缺失）。
11. `e2e_regressions/tests/bootstrap/network_layer/README.md`: 引用 `docs/ssot/bootstrap.network.md`（缺失）。
12. `docs/onboarding/03.database.md`: 文中路径使用 `2.platform/`，与当前目录结构不一致。
13. `docs/ssot/db.vault-integration.md`: 文中路径使用 `2.platform/`，与当前目录结构不一致。
14. `docs/onboarding/01.quick-start.md`: SigNoz 标记为“待部署”，状态不明确。
15. `docs/onboarding/01.quick-start.md`: OpenPanel 标记为“待部署”，状态不明确。
16. `docs/onboarding/06.observability.md`: SigNoz 标记为“待部署”，状态不明确。
17. `docs/onboarding/06.observability.md`: OpenPanel 标记为“待部署”，状态不明确。
18. `docs/ssot/bootstrap.nodep.md`: Dokploy 版本/日期未记录（版本表为空）。
19. `docs/ssot/bootstrap.nodep.md`: 1Password Connect 安装日期未记录。
20. `docs/ssot/bootstrap.nodep.md`: “Dokploy 服务可达”验证项为 Pending。
21. `docs/ssot/core.md`: “目录结构完整性”测试 `test_structure.py` 为 Planned。
22. `docs/ssot/ops.alerting.md`: `test_alert_channel.py` 为 Planned。
23. `docs/ssot/ops.observability.md`: `test_signoz_health.py` 为 Planned。
24. `docs/ssot/ops.recovery.md`: `test_backup_integrity.py` 为 Planned。
25. `docs/ssot/platform.ai.md`: `test_ai_injection.py` 待新建，且存在 TODO。
26. `docs/project/Infra-003.docs_reorg.md`: PR Links 为 TBD。
27. `docs/project/Infra-004.authentik_install.md`: PR Links 为 TBD。
28. `docs/project/Infra-004.authentik_install.md`: Change Log 为 TBD。
29. `docs/ssot/platform.auth.md`: 链接指向 `platform/5.casdoor.tf`，本仓库缺失该文件。
30. `docs/ssot/platform.secrets.md`: 链接指向 `tools/secrets/ci_load_secrets.py`，本仓库缺失该路径。
