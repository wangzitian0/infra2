# Infra2 合流门禁

> [AGENTS.md](../../AGENTS.md) 只保留判定所需的最小充分条件与 fail-closed 原则。
> 本文是逐条细则与其理由。任一状态失败、缺失或无法验证时必须 fail-closed，禁止合流。

## PR 提交准则

- **checklist**：要在 description 里面列 checklist。
- **检查 wiki 完备程度**：包括 SSOT、Project、README 等。
- **推送前检查**：确保没有冲突。已有的 Code Review 评论都已经处理（resolved）。

## 可合流条件（AI Merge，全部必需）

- **批准绑定当前 head**：PR 非 Draft，且满足下列两条路径之一。**逐-head 批准**：仓库 owner 已明确批准当前 `head SHA`；head 一旦变化该批准即失效，必须重新确认。**会话级授权**：该 PR 落在下文"会话级合流授权"的范围内；head 变化不需要回到 owner，但必须对新的 head 重新满足会话级的全部条件（含静置窗口重新计时）。
- **合流真源唯一**：目标分支正确，PR `mergeable`，无冲突；检查与合流必须针对同一个 `head SHA`，禁止用本地旧结果或旧 review 代替。
- **Merge Authority 全绿**：[`docs/ssot/ci-gate-inventory.yaml`](../../docs/ssot/ci-gate-inventory.yaml) 中该变更适用且 `blocks_merge: true` 的检查全部成功；pending、failure、cancelled、意外 skipped 或无法读取均视为不满足。
- **Review 已闭环（加权阻塞）**：required review 已满足，所有 actionable conversation / review threads 已处理并 resolved；不得自行忽略、dismiss 或用过期 review 代替当前 head 审查。未 resolved 的 review 发现（不论来源——human reviewer、Copilot、/code-review 等）按 severity 加权计分：high=1.0、middle=0.5、low=0.25，未标注 severity 的按 middle 计。**未 resolved 发现的加权总分 ≥ 1.0 即视为未闭环、禁止合流**，不要求单条 high 才阻塞——例如 2 条 middle 或 4 条 low 累计到位同样阻塞。达到门槛后必须逐条修复，或取得 owner 对具体发现的明确豁免并留痕，方可标记为已处理。
- **变更契约完整**：PR description checklist 完整；代码、测试、SSOT、Project、Layer README / Onboarding 按影响同步；无未解释的 scope drift；PR description 显式引用其推进/关闭的 issue 编号（无则写明 None）——避免 PR 实质推进了某 issue 的 scope 却不留痕迹，导致 issue 可见状态滞后仓库实际进度（#508）。
- **安全与运维门禁**：无敏感文件；已说明风险、回滚与 0 宕机影响；涉及 state discrepancy、密钥或生产数据时已按对应 SSOT 执行并留证。
- **高风险例外显式放行**：若 merge 本身会触发 apply / deploy（包括 L1 bootstrap self-update、尚未解耦的 observability apply）或有不可逆副作用，必须先完成变更专属 proof，并取得 owner 对该副作用的再次明确批准。
- **合流后闭环**：使用仓库允许的合流方式；确认 merge commit 已落在目标分支并监看 post-merge checks。失败时立即停止 tag / promote，报告并修复，不得继续发布。

## 会话级合流授权（2026-09-08 owner 批准）

逐-head 批准在当前节奏下不可执行：2026-09-08 单日合流 11 个 PR，其中多数在 Copilot review
被处理后 head SHA 变化，逐次回到 owner 确认会把交付停在等待上，而真正需要人看的两类改动
（触发部署、改受保护文件）反而淹没在其中。因此：

**owner 在一次会话中明确授权后**，AI 可自行合流同时满足下列全部条件的 PR：

- 上方"可合流条件"全部满足（Merge Authority 全绿、目标分支正确、变更契约完整、安全与运维门禁通过）；
- 所有 actionable review threads 已处理并 resolved（包括 Copilot 与 `/code-review`）；
- 该 head 已**静置**：自动 review（Copilot）已对**当前 head SHA** 提交且距该 review ≥ 3 分钟；若自动 review 迟迟不来，仍以距最后一次 push 12 分钟为上限（两者先到者为准）。fix-up push 不会自动触发 Copilot 复审，需要显式请求。判定与合流统一走 `python -m tools.pr_merge_gate <n> --policy either --request-review --merge`（exit 1 = 未到时机，exit 2 = 需要 owner），不再肉眼看表；
- 不触碰受保护文件（`AGENTS.md`、`CLAUDE.md`，以及各应用仓库标注为 protected 的架构文档）；
- 合并本身不触发 apply / deploy（含 L1 bootstrap self-update、`bootstrap/06.iac_runner/**` 触发的 runner 重建、尚未解耦的 observability apply）。

**仍需 owner 对当前 `head SHA` 明确批准**：触发部署或有不可逆副作用的 PR（上一条"高风险例外
显式放行"不因会话授权而放宽），以及修改受保护文件的 PR。

会话授权随会话结束而失效，不跨会话继承。

## 线上测试

- **Merge 与部署解耦**：普通开发 PR 以 `github_ci.merge_authority` 为合流门禁；preview / staging proof 不得冒充 Merge Authority，也不默认阻塞普通 PR。以 [`docs/ssot/ops.pipeline.md`](../../docs/ssot/ops.pipeline.md) 和 [`docs/ssot/delivery-stages.yaml`](../../docs/ssot/delivery-stages.yaml) 为准。
- **测试过程**：需要 runtime proof 时，请假设自己就是用户，从 web / cli / ssh 等真实入口验证，并保存证据。
- **发布与晋升**：release tag 必须来自 reviewed main；平台 tag 自动晋升 staging。staging 使用同一不可变 tag 验证并完成 soak 后，才可显式 promote prod，禁止跳过 staging。
