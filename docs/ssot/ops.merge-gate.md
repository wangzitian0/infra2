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
  可自行合流，剩下仍需 owner 的两类变更见文末。
- **合流真源唯一**：目标分支正确，PR `mergeable`，无冲突；检查与合流必须针对同一个 `head SHA`，禁止用本地旧结果或旧 review 代替。
- **Merge Authority 全绿**：[`docs/ssot/ci-gate-inventory.yaml`](ci-gate-inventory.yaml) 中该变更适用且 `blocks_merge: true` 的检查全部成功；pending、failure、cancelled、意外 skipped 或无法读取均视为不满足。
- **Review 已闭环（加权阻塞）**：required review 已满足，所有 actionable conversation / review threads 已处理并 resolved；不得自行忽略、dismiss 或用过期 review 代替当前 head 审查。未 resolved 的 review 发现（不论来源——human reviewer、Copilot、/code-review 等）按 severity 加权计分：high=1.0、middle=0.5、low=0.25，未标注 severity 的按 middle 计。**未 resolved 发现的加权总分 ≥ 1.0 即视为未闭环、禁止合流**，不要求单条 high 才阻塞——例如 2 条 middle 或 4 条 low 累计到位同样阻塞。达到门槛后必须逐条修复，或取得 owner 对具体发现的明确豁免并留痕，方可标记为已处理。
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
**仍需 owner 的两类，判据各不相同，分别列**：

- **其一，按环境划线：staging 可自行部署，prod 不可**（2026-09-21 owner 批准）。
  合流触发 staging 部署、临时槽 canary（`ops-checks.yml` 的 `deploy-v2-canary`，目标是保留的
  `pr-0`）、`report-branch-main` 预览重部署——**均属 AI 自行合流范围**，不需要逐次批准。
  触及 **prod** 的则必须取得 owner 对**当前 `head SHA`** 的明确批准，对旧 head 的批准不顺延：
  prod apply、prod promote、L1 bootstrap self-update、`bootstrap/06.iac_runner/**` 触发的
  runner 重建、尚未解耦的 observability apply（它直接打 live SigNoz）。
  判据是**打到哪个环境**，其次才是可逆性；两者冲突时以环境为准。
- **其二，改动"决定合流的东西"（判据与环境、可逆性均无关，是自我裁决问题）**：门禁从工作树读取规则，而 AI 合流自己的 PR
  时那就是该 PR 的分支——改动因此由它自己引入的版本审判。这是自我裁决问题，不是不可逆问题，
  所以单列。

  **范围由闭包算出，不由清单列举**（#787）：`tools/pr_merge_gate.self_governing_files()`
  从 `pr_merge_gate.py` 出发，BFS 它 import 的每个仓内模块、它读的数据文件，再并上每个成员
  自己的测试。新增一个 import 自动纳入保护，不靠谁记得去改清单。闭包到不了的只有两份规则
  散文（`AGENTS.md` 与本文件），它们显式列举。

  **放不放行按方向判，不按文件判**（2026-09-22 owner 指示：「方向可机械证明为收紧则放行。
  放松需要给我审核」）。被告改写法条的危害是单向的——对自己有利。只会让门禁更常说「不」的
  改动不可能对自己有利：

  | 改动 | 方向 | 判定 |
  |---|---|---|
  | `blocks_merge: true` 集合不变（新增 `false` 条目） | 非放松 | 门禁自行放行 |
  | 把一条 gate 提升为 `blocks_merge: true` | 收紧 | 门禁自行放行 |
  | 删除 / 降级一条 blocking gate | 放松 | **回 owner** |
  | 把一条 blocking gate 指向另一个 job | 放松（看着没删） | **回 owner** |
  | 改 `pr_merge_gate.py` 或任何闭包内 Python | 无机械读法，且无引用出口 | **回 owner** |
  | 改 `AGENTS.md` / 本文件 | 散文，无方向读法 | **引用 owner 指示 → 放行；否则回 owner** |
  | base 侧读不到 / 解析失败 / blocking 集合为空 | 证明不出来 | **回 owner** |

  证明由 `_proven_tighter()` 从 GitHub 读 base 与 head 两个版本算出，**不读工作树、不看 PR
  描述的声称**。一个 PR 里只要还有一个闭包文件证明不出来，整个 PR 仍回 owner——被证明的那个
  不替其余文件背书。

  **两份规则散文的第二条出口（2026-09-22 owner 指示：「授权给你 merge 权限啊。为什么卡我这？」）**：
  `AGENTS.md` 与本文件读不出方向，但可以换一种能验的证据——PR body 里引用 owner 那句指示。
  与下一条「受保护文件」不同，这条出口**由门禁自己核验**，不是人工核对的契约：
  `_owner_instruction_quoted()` 要求 body 里存在一行匹配
  `(?im)^(##+\s*)?(owner instruction|owner 指示)\b.*$`，且其后第一条非空行是引用
  （`>` 开头，或含「...」原话）——标题下面没有引用文本、或压根没有这个标题，都判不存在，维持
  回 owner。命中则这两份文件退出 `self_governing_files()` 的 unproven 集合，不再触发 exit 2；
  闭包里其余文件（`pr_merge_gate.py` 本身、它 import 的一切、它读的数据、这些文件各自的测试）
  不受影响——引用只对这两份散文生效，改代码本身仍回 owner，理由同上一条的"无机械读法"。
- **受保护文件**（`CLAUDE.md`，及各 App 标注为 protected 的架构文档）不再单独要求二次批准，
  但修改它们的 PR 必须在 description 中引用授权它的那句 owner 指令，使授权可追溯——这是 PR
  description 里的契约，由人核对，门禁不验（与上面 `AGENTS.md` / 本文件那条不同，那条门禁自验）。
  `AGENTS.md` 与本文件不算在这条里——它们的引用出口在上一条，由 `pr_merge_gate.py` 机械核验，
  不是这条的人工核对契约。
- **合流后闭环**：使用仓库允许的合流方式；确认 merge commit 已落在目标分支并监看 post-merge checks。失败时立即停止 tag / promote，报告并修复，不得继续发布。

## 授权沿革

2026-09-08 引入会话级合流授权，以绕开逐-head 批准的等待；2026-09-21 owner 以
「只要是标准 PR 过的，且各类 check 都过了，你可以 merge 代码」取代两者，改为上文的常设合流权。
2026-09-22 owner 澄清授权覆盖其名下全部仓库，这条事实随之上提到 workspace 级规则（dev_env#59）；
本文只保留 infra2 自己的门禁条件。文末仍需 owner 的第二类（改动"决定合流的东西"本身）不属于
权限保留，而是自我裁决问题：门禁从工作树读规则，AI 合流自己的 PR 时读到的就是该 PR 引入的版本。
判定与合流统一走 `python -m tools.pr_merge_gate <n> --policy either --request-review --merge`
（exit 1 = 未到时机，exit 2 = 需要 owner），不靠肉眼看表。

## 线上测试

- **Merge 与部署解耦**：普通开发 PR 以 `github_ci.merge_authority` 为合流门禁；preview / staging proof 不得冒充 Merge Authority，也不默认阻塞普通 PR。以 [`docs/ssot/ops.pipeline.md`](ops.pipeline.md) 和 [`docs/ssot/delivery-stages.yaml`](delivery-stages.yaml) 为准。
- **测试过程**：需要 runtime proof 时，请假设自己就是用户，从 web / cli / ssh 等真实入口验证，并保存证据。
- **发布与晋升**：release tag 必须来自 reviewed main；平台 tag 自动晋升 staging。staging 使用同一不可变 tag 验证并完成 soak 后，才可显式 promote prod，禁止跳过 staging。
