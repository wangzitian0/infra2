# Infra-006: TODOWRITE (Documentation Engineering)

**Status**: Active  
**Owner**: Infra

## Purpose
Track top issues discovered during documentation engineering.

## Top Issues (Top 30)
- [x] L0-L2 链接可达性/跳数报告（本地脚本）
- [x] PageRank/入度分析：用于调整 L1/L2 入口排序
- [x] 缩短不可达路径：补齐 Bootstrap 子目录与 E2E 子目录的入口链接
- [x] SSOT owner/proof 治理：新增 `docs/ssot/MANIFEST.yaml`，并用测试校验 README 索引、owner 文件、proof anchor、Project SSOT 链接不漂移
- [ ] SSOT HLS governance loop: track design -> metrics -> gradual gates -> threshold cleanup through finance_report issues #821-#824.
- [x] 两个生成索引的门禁挪进 `docs.yml`：输入全是文档的守卫必须被文档改动触发

## Latest Findings (2026-09-22)

**守卫跑不到它要守的改动**

- `docs/project/README.md`（由各 `Infra-NNN` 的 H1 + Status 生成）与 `docs/ssot/README.md`
  （由 `MANIFEST.yaml` 生成）的两个门禁，此前只在 infra-ci 的 `test-deployer-logic` 里，
  而该 job 的 `if:` 是 `has_non_doc == 'true'`。
- 实测：在工作树里加一个 `docs/project/Infra-999.demo.md` → `gen_project_index.py` exit=1；
  手改 `docs/ssot/README.md` 生成块里一行 → `gen_ssot_index.py` exit=1。**两种改动都是纯
  Markdown**，于是 `has_non_doc=false`，门禁与 `test_project_index_generated.py`
  （其 docstring 正写着「hand-editing the README index ... fails here」）一起被跳过。
- 也就是说 #505 那次漂移原样重来会全绿通过。`pr_merge_gate` 对纯 `.md` PR 的 skipped
  required checks 是**设计内接受**的（否则这类 PR 永远不可合流），所以没有第二道拦得住。
- 修复：两个生成器加进 `docs.yml`（触发条件 `**/*.md` + `docs/**`）。`pr_merge_gate` 的
  `not_green` 扫描**每一个**报告过的 check，所以 `Docs` 变红即阻断合流，不需要动
  `ci-gate-inventory.yaml`。
- 覆盖是闭的而不是靠列举：任何改动集要么含非 `.md` 文件（infra-ci 跑）、要么命中
  `**/*.md` 或 `docs/**`（docs.yml 跑），两者的并集没有缝。
  `libs/tests/test_docs_only_prs_run_their_own_gates.py` 按象限枚举形状逐个断言，
  且 `has_non_doc` 不是重新实现的——把 infra-ci 那段 shell 取出来在临时仓库上**真的执行**。
- 反向验证：撤掉 docs.yml 的改动后 8 红 6 绿（红的正好是 4 种纯文档形状 × 2 个生成器），
  加回后 14 全绿。

## Latest Findings (2026-06-11)

**SSOT HLS Governance**
- Added Infra-006 as-is/to-be checklist for incremental SSOT high-level structure governance.
- Linked finance_report issues #821-#824 as the cross-repository governance loop.
- Kept this step documentation-only; no SSOT owner migration or gate behavior changes.

## Latest Findings (2026-06-10)

**SSOT Governance**
- Added machine-readable SSOT manifest.
- Added tests for manifest owner/proof reachability.
- Added tests for README SSOT key parity with manifest.
- Added tests for Project docs linking only to existing SSOT files.

## Latest Findings (2025-12-31)

**Reachability**
- TOTAL_MD: 70
- DIST_LEVELS: {0: 1, 1: 15, 2: 42, 3: 12}
- UNREACHABLE: 0

**PageRank Top 10**
1. docs/ssot/README.md
2. docs/onboarding/02.first-app.md
3. docs/onboarding/05.sso.md
4. docs/onboarding/03.database.md
5. docs/ssot/db.overview.md
6. docs/README.md
7. docs/onboarding/README.md
8. AGENTS.md
9. docs/project/README.md
10. docs/ssot/ops.recovery.md
