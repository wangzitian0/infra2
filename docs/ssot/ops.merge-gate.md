# Infra2 合流门禁

> [AGENTS.md](../../AGENTS.md) 只保留判定所需的最小充分条件与 fail-closed 原则。
> 本文是逐条细则与其理由。任一状态失败、缺失或无法验证时必须 fail-closed，禁止合流。

## PR 提交准则

- **checklist**：要在 description 里面列 checklist。
- **检查 wiki 完备程度**：包括 SSOT、Project、README 等。
- **推送前检查**：确保没有冲突。已有的 Code Review 评论都已经处理（resolved）。

## 可合流条件（AI Merge，全部必需）

- **常设合流权**是 workspace 级事实（工作区根 `AGENTS.md`「合流授权」），本文不重述其范围；
  本文只定义 infra2 自己的门禁：标准 PR 流程走完、下列**客观可判定**条件全部满足的 PR，AI
  可自行合流，only a production deployment still needs the owner (see the end of this list).
- **合流真源唯一**：目标分支正确，PR `mergeable`，无冲突；检查与合流必须针对同一个 `head SHA`，禁止用本地旧结果或旧 review 代替。
- **Merge Authority 全绿**：[`docs/ssot/ci-gate-inventory.yaml`](ci-gate-inventory.yaml) 中该变更适用且 `blocks_merge: true` 的检查全部成功；pending、failure、cancelled、意外 skipped 或无法读取均视为不满足。
- **Review 已闭环（加权阻塞）**：required review 已满足，所有 actionable conversation / review threads 已处理并 resolved；不得自行忽略、dismiss 或用过期 review 代替当前 head 审查。未 resolved 的 review 发现（不论来源——human reviewer、Copilot、/code-review 等）按 severity 加权计分：high=1.0、middle=0.5、low=0.25，未标注 severity 的按 middle 计。**未 resolved 发现的加权总分 ≥ 1.0 即视为未闭环、禁止合流**，不要求单条 high 才阻塞——例如 2 条 middle 或 4 条 low 累计到位同样阻塞。达到门槛后必须逐条修复；判定为误报时，在线程上写出可核验的理由，并把该关切变成一条可证伪的不变量测试，再 resolve（2026-10-06 起不再走 owner 豁免）。
- **绿必须是当前的**：检查只证明它跑过的那棵树。兄弟 PR 合入后改写了本 PR 也动的文件时，
  所有检查**仍然是绿的**——因为没有一个重跑过。因此必须比较 `base...head`，交集非空即更新分支重跑。
- **必需检查必须在场**：`blocks_merge: true` 的检查不仅要绿，还必须**确实报告过**。
  `detect-changes` 失败时其下游 job 是 skipped 而非 run，GitHub 仍接受为已满足——
  因此必需集须以 `ci-gate-inventory.yaml` 为准逐个核对在场与结论，而不是只看已报告的检查是否为绿。
- **静置**：自动 review（Copilot）已对**当前 head SHA** 提交且距该 review ≥ 3 分钟；
  自动 review 迟迟不来时以距最后一次 push 12 分钟为上限（先到者为准）。
  fix-up push 不会自动触发 Copilot 复审，需显式请求。
- **变更契约完整**：PR description checklist 完整；代码、测试、SSOT、Project、Layer README / Onboarding 按影响同步；无未解释的 scope drift；PR description 显式引用其推进/关闭的 issue 编号（无则写明 None）——避免 PR 实质推进了某 issue 的 scope 却不留痕迹，导致 issue 可见状态滞后仓库实际进度（#508）。
- **安全与运维门禁**：无敏感文件；已说明风险、回滚与 0 宕机影响；涉及 state discrepancy、密钥或生产数据时已按对应 SSOT 执行并留证。
**Only one class needs the owner: a production deployment** (owner instruction, 2026-10-06: "只有 prod 环境部署需要我批准和在场。其他的事情 agent 都可以自己干，但是需要满足每个 stage 的 checklist（部分是文字描述、部分是 skill、部分是门禁）。").

- **Draw the line by environment.** A merge that triggers a staging deploy, the reserved-slot canary (`ops-checks.yml` `deploy-v2-canary`, slot `pr-0`) or the `report-branch-main` preview redeploy is the agent's merge. A merge that reaches **production** needs the owner's approval of the current `head SHA`, and an approval of an older head does not carry over. Production today means: prod apply, prod promote, L1 bootstrap self-update, a runner rebuild triggered by `bootstrap/06.iac_runner/**`, and the observability apply that writes live SigNoz. One definition of production is pending (#1035). The gate reports these merges as exit 2.
- **A change to what decides merges is not an owner case** (#1040). The preflight (#873) refuses a gate checkout whose rules differ from main, so main's rules judge every PR and no PR judges itself. `tools/pr_merge_gate.self_governing_files()` computes the closure: `pr_merge_gate.py`, every in-repo module it imports, the data files it reads, and each member's tests, plus the two rule texts (`AGENTS.md` and this file). A change to the closure passes the **gate-change checklist**:

  | Item | Kind | Gate result when missing |
  |---|---|---|
  | The verdict comes from a gate checkout equal to main | gate (preflight) | exit 4, pull main |
  | A direction proof passes (`_proven_tighter()`, computed from base and head on GitHub) | gate | the next two items apply |
  | An automated review of the current head | gate | exit 1, wait for the review |
  | A `### Mutation evidence` section in the PR body that names the tests that fail under a relevant mutation | text, checked for presence by `_mutation_evidence_listed()` | exit 3 |
  | Rule text changes in its dev_env source and passes the publish guards | text and dev_env CI | not merged in dev_env |

  The direction proof stays the fast path. It covers only closed-set inputs (the CI gate inventory, workflow job names and push paths). A change that the proof cannot read is not refused; it needs the review and the evidence instead. A proven file does not cover an unproven file in the same PR.
- **The reservation guards itself.** Some files decide whether a merge reaches production, or pin the text that defines the reservation: `PRODUCTION_GUARD_FILES` in `libs/gate/types.py` (the gate's decision code and `libs/tests/test_production_reservation.py`) and every workflow, which carries the deploy triggers. An unproven change to one of them needs the owner's approval of the head SHA (exit 2). The checklist above does not clear it, because a relaxation there could let a later production merge pass without the owner. `libs/tests/test_production_reservation.py` pins the reservation text in this file, so narrowing that text also needs the owner.
- **Protected files.** `CLAUDE.md` and `AGENTS.md` are generated from the dev_env rule source; change the source, not the file. An App's protected architecture docs follow that App's own checklist. A quoted owner instruction is not required.
- **合流后闭环**：使用仓库允许的合流方式；确认 merge commit 已落在目标分支并监看 post-merge checks。失败时立即停止 tag / promote，报告并修复，不得继续发布。
  **但 `cancelled` 在合流前与合流后不是同一个含义**：作为 Merge Authority 一律算不满足（见上）；
  **合流后**监看时，`infra-ci.yml` 设了 `concurrency: cancel-in-progress: true`，兄弟 PR 随后合入
  main 会正当地顶替在途那一轮——这种 cancelled 不是失败，不触发上面那句「立即停止 tag / promote」。
  判据是**回去读该 workflow 的 `concurrency` 配置、确认它是否被更晚的 push 顶替**，不从状态字面
  推断（2026-09-23 实测：#815 合入后 run 报 cancelled，实为 #827 数秒后合入顶替，最新主干头三条
  检查全绿）。

## 授权沿革

2026-09-08 引入会话级合流授权，以绕开逐-head 批准的等待；2026-09-21 owner 以
「只要是标准 PR 过的，且各类 check 都过了，你可以 merge 代码」取代两者，改为上文的常设合流权。
2026-09-22 owner 澄清授权覆盖其名下全部仓库，这条事实随之上提到 workspace 级规则（dev_env#59）；
本文只保留 infra2 自己的门禁条件。2026-10-06 owner 重申：只有 prod 部署需要 owner 批准和在场；
改动"决定合流的东西"不再回 owner，改由 gate-change checklist 把关（#1040）——#873 的 preflight
保证门禁只用 main 的规则判案，PR 不能再用自己引入的规则审判自己。
判定与合流统一走 `python -m tools.pr_merge_gate <n> --policy either --request-review --merge`
，不靠肉眼看表。Act on the exit code only (#740):

| Exit | Meaning | Next step |
|---|---|---|
| 0 | Ready (merged with `--merge`). | Continue from the latest main. |
| 1 | Wait. Time alone fixes it: a check is pending, no check has reported yet, or the head is settling. | Wait and re-run. |
| 2 | Needs the owner. | Ask the owner about this head SHA. |
| 3 | Action required. Waiting does not fix it: a red or cancelled check, an open review thread, a conflict, a draft, a PR that is not open, or a base that changed under green checks. | Fix the cause, push, re-run. |
| 4 | Could not evaluate. This is not a verdict on the PR. The gate checkout's rules differ from main's (#873), or `gh` failed or returned unreadable data. The preflight finds a stale checkout before it reads any PR state. | Pull main in the gate checkout, or retry after the `gh` failure. |

## 线上测试

- **Merge 与部署解耦**：普通开发 PR 以 `github_ci.merge_authority` 为合流门禁；preview / staging proof 不得冒充 Merge Authority，也不默认阻塞普通 PR。以 [`docs/ssot/ops.pipeline.md`](ops.pipeline.md) 和 [`docs/ssot/delivery-stages.yaml`](delivery-stages.yaml) 为准。
- **测试过程**：需要 runtime proof 时，请假设自己就是用户，从 web / cli / ssh 等真实入口验证，并保存证据。
- **发布与晋升**：release tag 必须来自 reviewed main；平台 tag 自动晋升 staging。staging 使用同一不可变 tag 验证并完成 soak 后，才可显式 promote prod，禁止跳过 staging。
