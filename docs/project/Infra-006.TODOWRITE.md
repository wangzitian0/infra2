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
- 修复：两个生成器加进 `docs.yml`（触发条件 `**/*.md` + `docs/**`）。
- **这条阻断到什么程度，起初写宽了，`/audit` Scout-T 打假后改正**：
  `pr_merge_gate` 的 `not_green` 扫描**每一个**报告过的 check（不只 required 的），
  所以**走 `pr_merge_gate` 合流时**这个 check 变红即阻断——它报出来的名字是 **`build`**，
  不是 `Docs`（docs.yml 的 job 没有 `name:`，grep `Docs` 什么也找不到）。
  **但 GitHub 自己不拦**：main 的 ruleset（`11416804`，实测）只要求 7 个 check，
  `build` 不在其中（`bypass_actors=0`、`current_user_can_bypass=never`，那 7 个是真拦的）。
  Web UI 或 `gh pr merge` 直接合并不会被拦住。这是**纪律**而不是**机制**，
  恰恰是同一批 PR 自己反对的那种闭合方式。
- **而「登记进 ruleset」不是解法，已实测证否**：`docs.yml` 只在 `**/*.md`/`docs/**` 上触发，
  把它的 `build` 声明为 `blocks_merge: true` 后，纯代码 PR 会因为它**从未报告**而被拦死——
  直接跑 `pr_merge_gate.evaluate` 验证：`['required check(s) never reported: build']`。
  GitHub ruleset 侧同理（带 `paths:` 过滤的 workflow 一旦成为 required check，
  不匹配的 PR 会永远停在 Expected）。而 `ci-gate-inventory.yaml` 的 gate schema 里
  **没有**路径适用性字段（字段只有 `id/stage/task_category/workflow/job/blocks_merge/failure_semantics`）。
  所以真正的补齐要么是把门禁挪进一个**无条件运行**的 job，要么去掉 docs.yml 的 `paths:`，
  要么给 gate schema 加适用性——三条都属于「改动决定合流的东西」，**需 owner**。
- **门禁被拆掉这件事合不进去——但机制分两种，我起初只说了一种，而且说错了那一种**
  （第二轮 claim 审计打假，已实测）：
  - **只改 `.github/workflows/docs.yml` 一个文件**（正是「拆门禁」的形状）：
    该路径**不在** infra-ci 的 `paths:` 里（实测 25 条里 5 条 workflow 条目，无 docs.yml），
    所以 **infra-ci 根本不触发**，`has_non_doc` 从未被计算，`Test Deployer Hash Logic`
    也不会跑——**不是「跑了并抓到」，是「根本没跑」**。它仍然合不进去，但靠的是七个
    required check **一个都没报告**：`pr_merge_gate` 实测给出
    `required check(s) never reported: <七个全部>`，GitHub ruleset 侧则永远停在 Expected。
  - **PR 同时动了 infra-ci 允许清单里的东西**（`libs/**`、`tools/**`、`docs/**`…，
    历史上每一次改 docs.yml 都是这种形状）：infra-ci 触发，`Test Deployer Hash Logic`
    跑 `libs/tests`，`test_docs_only_prs_run_their_own_gates` 在步骤被删/`|| true`/
    `continue-on-error` 时都红（实测 3/3/4 红）。这一种才是**被测试抓到**。
  - 我原来的说法把第一种形状也归给了测试。**错法正是同一段文字在另一处刚警告过的那条**：
    带 `paths:` 过滤的 required check，在不匹配的 PR 上永远停在 Expected。
    写下这个陷阱，然后在下一段掉进去。
  - 顺带一条：`libs/tests/test_docs_only_prs_run_their_own_gates.py` 的**代码**是对的——
    它在问 `has_non_doc` 之前先问 `_workflow_fires(INFRA_CI, files)`（实测该文件下为
    `False`）。错的只有散文：我把分类器**单独**拿来推论，而测试没有。
- 剩余敞口只有一种：**纯文档 PR 索引真的过期、`build` 真的红了，而有人不走
  `pr_merge_gate` 直接从 UI 合并**——那时 GitHub 不拦。
- 覆盖的范围是**两个生成器读的那些文件**，不是任意改动集：`docs/project/**.md`、
  `docs/project/README.md`、`docs/ssot/MANIFEST.yaml`、`docs/ssot/README.md`。
  这些全在 `docs/` 下，而 `docs/**` 同时在 infra-ci 的 `paths:` 允许清单和 docs.yml 的触发条件里，
  所以每一个都至少被一个 workflow 接住。
- **独立只读审计（Sonnet，未看过缺陷上下文）抓到本 PR 两条，都成立，都已改**：
  1. **HIGH**——第一版断言只是「生成器路径出现在步骤文本里」。审计员把 docs.yml 的步骤改成
     `python3 tools/gen_project_index.py || true`（门禁彻底失效），**23 个测试仍然全绿**。
     这正是 #776 的姊妹测试点名要防的形态，而这个测试自己没防。已改为**执行式**：
     造一棵索引过期的树（只弄脏目标生成器的输入，另一个保持同步，以此证明失败是**穿过**
     步骤传出来的而不是来自它的兄弟命令），把步骤的 `run:` 正文真的跑一遍（`uv` 用 shim 换成
     `python3`，这样 `&&`／`||`／`set +e`／提前 `exit 0` 这些 shell 逻辑都在被测量），
     断言退出码非 0；`continue-on-error` 另按 YAML key 查。
  2. **MIDDLE**——「任何改动集都逃不过两个 workflow」这句是**假的**：infra-ci 的 `paths:` 是
     一份策划过的允许清单，`pyproject.toml` 既不在里面、也不命中 docs.yml，两个 workflow 都不触发。
     （它改不了这两个索引，且 `pr_merge_gate` 会以「required check(s) never reported」拦下这种 PR，
     所以不是可利用的洞——但结论写宽了就是错的。）已把 SSOT、本条与测试 docstring 的说法
     收敛到「生成器读的文件」。
- 反向验证（改完后逐个复现审计员的攻击）：
  | 怎么废掉门禁 | 测试结果 |
  |---|---|
  | 步骤整个删掉（原始缺陷） | 3 红 |
  | 步骤加 `\|\| true` | 3 红 |
  | 步骤加 `continue-on-error: true` | 4 红 |
  | 原样 | 15 全绿 |

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
