# Infra2 合流门禁

> [AGENTS.md](../../AGENTS.md) 只保留判定所需的最小充分条件与 fail-closed 原则。
> 本文是逐条细则与其理由。任一状态失败、缺失或无法验证时必须 fail-closed，禁止合流。

## PR 提交准则

- **checklist**：要在 description 里面列 checklist。
- **检查 wiki 完备程度**：包括 SSOT、Project、README 等。
- **推送前检查**：确保没有冲突。已有的 Code Review 评论都已经处理（resolved）。

## 可合流条件（AI Merge，全部必需）

- **常设合流权（2026-09-21 owner 批准，取代逐-head 与会话级授权）**：标准 PR 流程走完、
  下列条件全部满足的 PR，AI 可自行合流；不需要 owner 逐个批准，也不需要每个会话重新授权。
  逐-head 批准在实际节奏下把交付停在等待上（2026-09-08 单日 11 个 PR，多数在 Copilot review
  后 head 变化），而真正需要人看的那一类反而淹没其中；会话级授权只是把同一问题挪到每个会话开头。
  保留的是下面这些**客观可判定**的条件，以及唯一一类仍需 owner 的变更（见文末）。
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
- **按环境划线：staging 可自行部署，prod 不可**（2026-09-21 owner 批准）。
  合流触发 staging 部署、临时槽 canary（`ops-checks.yml` 的 `deploy-v2-canary`，目标是保留的
  `pr-0`）、`report-branch-main` 预览重部署——**均属 AI 自行合流范围**，不需要逐次批准。
  触及 **prod** 的则必须取得 owner 对**当前 `head SHA`** 的明确批准，对旧 head 的批准不顺延：
  prod apply、prod promote、L1 bootstrap self-update、`bootstrap/06.iac_runner/**` 触发的
  runner 重建、尚未解耦的 observability apply（它直接打 live SigNoz）。
  判据是**打到哪个环境**，其次才是可逆性；两者冲突时以环境为准。
- **改动"决定合流的东西"也需 owner（与可逆性无关）**：门禁从工作树读取规则，而 AI 合流自己的 PR
  时那就是该 PR 的分支——改动因此由它自己引入的版本审判。这是自我裁决问题，不是不可逆问题，
  所以单列。`tools/pr_merge_gate.py`、它的测试、`ci-gate-inventory.yaml` 以及本文件属于这一类。
- **受保护文件**（`AGENTS.md`、`CLAUDE.md`，及各 App 标注为 protected 的架构文档）不再单独要求
  二次批准，但修改它们的 PR 必须在 description 中引用授权它的那句 owner 指令，使授权可追溯。
- **合流后闭环**：使用仓库允许的合流方式；确认 merge commit 已落在目标分支并监看 post-merge checks。失败时立即停止 tag / promote，报告并修复，不得继续发布。

## 授权沿革

2026-09-08 引入会话级合流授权，以绕开逐-head 批准的等待；2026-09-21 owner 以
「只要是标准 PR 过的，且各类 check 都过了，你可以 merge 代码」取代两者，改为上文的常设合流权。
判定与合流统一走 `python -m tools.pr_merge_gate <n> --policy either --request-review --merge`
（exit 1 = 未到时机，exit 2 = 需要 owner），不靠肉眼看表。

## 线上测试

- **Merge 与部署解耦**：普通开发 PR 以 `github_ci.merge_authority` 为合流门禁；preview / staging proof 不得冒充 Merge Authority，也不默认阻塞普通 PR。以 [`docs/ssot/ops.pipeline.md`](ops.pipeline.md) 和 [`docs/ssot/delivery-stages.yaml`](delivery-stages.yaml) 为准。
- **测试过程**：需要 runtime proof 时，请假设自己就是用户，从 web / cli / ssh 等真实入口验证，并保存证据。
- **发布与晋升**：release tag 必须来自 reviewed main；平台 tag 自动晋升 staging。staging 使用同一不可变 tag 验证并完成 soak 后，才可显式 promote prod，禁止跳过 staging。
