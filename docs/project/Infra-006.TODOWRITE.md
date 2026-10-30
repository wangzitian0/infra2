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
- [x] 生成索引的输入判为配置而非散文：`detect-changes` 把 `docs/project/**.md` 与 `docs/ssot/README.md` 归 non-doc

## Latest Findings (2026-09-22)

**结论先行：最终落地的是「改一条分类规则」，不是「写 700 行模拟 CI」。**

最终方案：`detect-changes` 把 `docs/project/**.md` 与 `docs/ssot/README.md` 判为 non-doc，于是
`test-deployer-logic`（GitHub ruleset 强制的七个必需检查之一）会跑，而**早就存在**的
`test_project_index_generated` / `test_ssot_index_generated` 就是守卫。实测这两个测试对三种漂移
（新增未登记文档、Status 就地漂移、手改生成块）全部报红——守卫一直有效，缺的只是调度。

新测试 `test_generated_doc_indexes_are_guarded.py` 只证明三件事，共 8 个用例：
生成器的每个输入都不被判为 doc-only（输入清单从生成器常量**推导**，不是手写）；
普通散文仍是 doc-only（对照，防止规则宽到失去意义）；
两个生成器都能检测**不改行数的就地修改**（因为守卫测试只读退出码，生成器的比较逻辑才是被信任的那一层——
盲审实测把 `gen_ssot_index` 的全文比对削弱成行数比对不会被任何既有测试抓到）。

**为什么放弃第一版**：它在 `docs.yml` 加门禁步骤 + 700 行测试模拟 CI 的 job 调度与步骤执行。
六轮对抗审计打穿 **19 次**。每修一轮就换一把同一套 YAML schema 的钥匙再开。
根因不是断言不够强，是**输入面选错了**：模拟 CI 的输入是 GitHub Actions 的全部语义（开集），
而分类规则的输入是四条路径模式（闭集）。**「这个 job 会不会跑」是 GitHub 的职责，
而 GitHub 恰好是这条链上唯一不会搞错它的组件。**

**代价已量化**：300 个 PR / 97 天里，新规则让 5 个原本 doc-only 的 PR 多跑一次
约 2 分钟的 job（其中 3 个是 TODOWRITE，属于刻意保守的误伤）。公开仓库 runner 免费。

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
