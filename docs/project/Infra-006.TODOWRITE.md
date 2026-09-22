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
  不是 `Docs`：docs.yml 的 **job** 没有 `name:`，GitHub 就用 job id 作 check context。
  （文件顶层确实有 `name: Docs`，但那是 **workflow** 名，不是 check 名——第四轮 claim
  审计指出原先那句「grep `Docs` 什么也找不到」说过头了。）
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
- **第五轮盲审又打穿两次，又都是 HIGH**。这一轮尤其说明问题：**每次我以为闭合了，
  换一把 YAML 钥匙就又开了**——先是步骤文本，再是 job 条件，这次是 `needs:` 和 `shell:`。
  1. **`needs:` 从不检查**。给 `test-deployer-logic` 加一个依赖（`needs: [detect-changes, lint-python]`，
     看起来就是「lint 过了再跑部署测试」的排序优化）——那个依赖一旦失败或被跳过，
     本 job 连同它的 15 个门禁整个 skip，而 **skipped 被 branch protection 当作满足**。测试全绿。
  2. **`shell:` 覆盖从不复制**。`shell: bash {0}` 去掉 `-e`，多行脚本只报**最后一行**的退出码，
     前面失败的生成器被吞掉。测试全绿，因为 `_run_step` 硬编码 `bash -e`。
     **实测同一棵过期树**：`bash -e` exit 1 / `bash` exit 0。而且它精确重开 #505 的文档-only 路径——
     纯文档 PR 只到得了 docs.yml 这一个门禁，`gen_project_index` 在第一行失败、
     `gen_ssot_index` 在第二行成功，脚本退出码 0，CI 绿。
     更隐蔽的变体是 job 级 `defaults.run.shell`，连门禁步骤上都看不见。
- 修法：
  - `needs:` 和 `if:` 一样钉进 `GATE_JOBS` 并断言相等（同样是锁定不是求值，理由同上）。
  - `_run_step` 不再硬编码，改为**解析 workflow 声明的 shell**（step → job `defaults.run` →
    workflow `defaults.run`），按 GitHub 文档的映射构造 argv；**未建模的 `shell:` 值一律断言失败**，
    宁可拒绝也不要在另一个 shell 下测量。姊妹的 pi 测试同步加了「没有 shell 覆盖」的断言。
- 反向验证：`needs` 加依赖 **1 红**、step `shell` 覆盖 **4 红**、job `defaults.run.shell` **4 红**、
  未建模 shell（`shell: python`）**8 红**（fail-closed 生效）、pi 步骤 shell 覆盖 **3 红**；原样 22 / 3 全绿。

- **第四轮盲审又打穿两次，都是 HIGH，都成立**：
  1. **job 级 `if:` 从不检查**。把 `test-deployer-logic` 或 docs.yml 的 `build` 整个 job
     `if: false`——门禁连同该 job 里另外十来个必需检查一起关掉——**测试全绿**。
     `GATE_JOBS` 硬编码了 job 名，却从不读 `job["if"]`：这套测试证明的是
     「**这些步骤如果被执行**，行为正确」，从来不是「**这个 job 会被执行**」。
  2. **同 job 的前序步骤能用 `$GITHUB_PATH` 把影子 `uv`/`python3` 塞到 PATH 前面**，
     让其后**每一个**步骤（不止这两个门禁）静默空转——**测试全绿**。
     因为测试是孤立执行单个步骤，不重放整个 job 的步骤间环境传播。
- 修法与它们的性质：
  - `GATE_JOBS` 改为 `(workflow, job, 期望的 if 表达式)`，新增测试断言实际 `if:` **恰好**等于模型。
    这是**文本锁定不是求值**——求值等于在这里重新实现一遍 GitHub 表达式语言，
    而一个实现错的判官比一个「对不上就红、逼人来同步」的锁更危险。注释里写明了这个取舍。
  - 跨步骤环境劫持：扫描 gate 步骤**之前**的同 job 步骤，禁止出现
    `GITHUB_PATH`/`GITHUB_ENV`/`add-path`/`set-env`。这也是文本匹配，但匹配的是
    **GitHub 自己定义的、有限的、文档化的**全部跨步骤环境机制——**闭集**，
    不同于 shell 动词那种开集。这个区别是这条守卫能不能成立的全部理由。
- 同轮 LOW：`gen_project_index` 只测了「新增未登记文档」，没测「已有文档 Status 漂移」——
  而 #505 两种都有（Infra-004/005 就是后者）。已补 `status` 模式 fixture。
- **第四轮 claim 审计：零 FALSE。** 六个变异数字、`core.fsmonitor`、`GIT_AUTHOR_*` 覆盖
  `-c user.*`、ruleset 七个 check、`pr_merge_gate` 的两个反事实、2064、九个门禁，全部独立复现。
  唯一一条是上面那个括号的措辞，已改。

- **第三轮盲审（只喂代码，屏蔽全部 `*.md` 与 `docs/`）把这个测试打穿了两次，都成立**：
  1. 把 docs.yml 的步骤正文换成**无条件 `exit 1`**（注释里仍写着两个生成器的名字）——
     门禁彻底没了、每个文档 PR 都红，**19 个用例照样全绿**。因为断言只有
     `returncode != 0`，一个永远红的假门禁轻松满足它。**「红」和「会判断」不是一回事。**
  2. `gen_project_index` 的两个输入都是纯 `.md`，于是 `has_non_doc=false`，
     infra-ci 里那份同样的门禁**从来没被这个套件碰到过**。给它加 `|| true`——
     正是本文件 docstring 点名拒绝的那种写法——**15 个用例全绿**。
- 两条的修法是同一个：**断言必须能分辨**。现在每个被触达的门禁都跑**两遍**：
  对一棵**索引同步**的树必须 exit 0，对一棵**索引过期**的树必须 exit 非 0；
  而且是对**每一个**被触达的门禁都要求（不是 `any`）——一个写着生成器名字却吞掉它退出码的
  步骤就是假的，旁边有没有另一个门禁兜着都一样。参数化新增 `mixed`（输入 + 一个非文档文件），
  把 infra-ci 那份也拉进射程。
- 同轮还有一条 MIDDLE：`core.fsmonitor` 是和 gpgsign/hooksPath 同族的**第四个**挂起点，
  三个 `-c` 都钉了也没钉它（实测：其余三个都在的情况下 `git add -A` 仍被阻塞）。
  已加 `-c core.fsmonitor=false`。另外 `_sanitized_env` 原本只剔三个名字，而
  `GIT_AUTHOR_*`/`GIT_COMMITTER_*` 能直接盖过 `-c user.*`、`GIT_CONFIG_GLOBAL` 等同族还有一串——
  **名单是开集，前缀是闭集**，改成剔掉整个 `GIT_*`。
- 反向验证（改完后逐个复现审计员的攻击）：
  | 怎么废掉门禁 | 测试结果 |
  |---|---|
  | 步骤整个删掉（原始缺陷） | 3 红 |
  | 步骤加 `\|\| true` | 4 红 |
  | 步骤加 `continue-on-error: true` | 8 红 |
  | 步骤换成无条件 `exit 1`（三轮盲审攻击，此前 19 绿） | **8 红** |
  | infra-ci 的 project-index 门禁加 `\|\| true`（三轮盲审攻击，此前 15 绿） | **2 红** |
  | infra-ci `test-deployer-logic` 整个 job `if: false`（四轮盲审攻击，此前 19 绿） | **1 红** |
  | docs.yml `build` 整个 job `if: false`（四轮盲审攻击，此前 19 绿） | **1 红** |
  | 前序步骤用 `$GITHUB_PATH` 塞影子（四轮盲审攻击，此前 19 绿） | **5 红** |
  | 原样 | 22 全绿 |
  | 敌对 `~/.gitconfig`（fsmonitor + gpgsign 双挂起） | 22 绿（未加固时 `add -A` 8 秒被杀） |

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
