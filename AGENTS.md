# Infra2 Harness 与基础设施 AI Agent 行为准则

> **权限边界**：AI 修改本文件需 owner 明确指示，并在 PR description 中引用那句指示。AI 可在"合流门禁"全部满足后自行 Merge PR（2026-09-21 owner 授予常设合流权，取代逐-head 与会话级授权，细则见 [`docs/ssot/ops.merge-gate.md`](docs/ssot/ops.merge-gate.md)）；任一状态失败、缺失或无法验证时必须 fail-closed，禁止合流。

> **本文只放判定所需的不变量。** 程序性细则按需加载，入口见下方"按需加载"。
> 长期常驻的指令会稀释红线的权重——规则越长越不被遵守。

## Harness 作用域与优先级

本仓库同时是 `infra2` 的实现/部署控制面和多仓库开发 workspace。机器清单见
[`harness/repos.yaml`](harness/repos.yaml)，架构边界见
[`docs/ssot/core.harness.md`](docs/ssot/core.harness.md)。

1. **Harness focus**：本文件直接治理根目录的 `infra2` 工作；workspace 重点是
   `infra2`、`infra2-sdk` 与通用协作偏好。
2. **App 自治**：`repos/finance_report` 与 `repos/truealpha` 只是 workspace checkout。
   进入 App 后，必须先读其本地 `AGENTS.md` 与架构文档；App 本地规则优先，harness
   不复制、不分发、不强制同步 App policy。
3. **Workspace tooling**：根目录 `oh-my-code-agent/` 是独立 submodule，用于逐步承载
   各类 TUI 管理。它独立迭代，不得成为 infra 或 App 的 runtime/source 依赖。
4. **偏好不是跨仓库命令**：GitHub、协作与软件设计默认偏好位于
   [`harness/workspace/`](harness/workspace/)。目标仓库有更具体规则时，以目标仓库为准。
5. **依赖边界**：submodule 只表示开发快照，不是 package、runtime、deployment 或
   config-hash 依赖。稳定跨仓库代码契约只通过已发布的 `infra2-sdk` 版本传递。

## 🚨 核心强制原则（SSOT First）

1. **SSOT 为最高真理**：基础设施的**唯一权威来源**是 [`docs/ssot/`](docs/ssot/README.md)。
2. **无 SSOT 不开工**：引入新组件前，必须先在 `docs/ssot/` 定义其真理（架构、约束、SOP）。
3. **禁止隐性漂移**：发现代码与 SSOT 不符时必须立即同步修正，严禁让 SSOT 腐烂。

## 🔒 合流门禁（最小判定条件）

完整细则见 [`docs/ssot/ops.merge-gate.md`](docs/ssot/ops.merge-gate.md)。
**下列任一不满足即 fail-closed，禁止合流**：

1. **同一 head**：检查、review、合流针对同一个 `head SHA`；不得用本地旧结果或过期 review 代替。
2. **Merge Authority 全绿**：[`docs/ssot/ci-gate-inventory.yaml`](docs/ssot/ci-gate-inventory.yaml) 中适用且 `blocks_merge: true` 的检查全部 success；pending / failure / cancelled / 意外 skipped / 读不到，都算不满足。
3. **Review 已闭环**：未 resolved 发现按 severity 加权（high=1.0 / middle=0.5 / low=0.25，未标注按 middle），**总分 ≥ 1.0 即禁止合流**。
4. **绿是当前的，且必需检查确实报告过**：兄弟 PR 合入后改写了本 PR 也动的文件时，检查仍是绿的
   却没重跑；`blocks_merge` 的检查被 skip 时 GitHub 也接受为已满足。判定统一走
   `python -m tools.pr_merge_gate <n> --policy either --request-review --merge`
   （exit 1 = 未到时机，exit 2 = 需要 owner），不靠肉眼看表。
5. **两类必须回到 owner**，判据各不相同：
   **(a) 回滚撤销不了**——L1 bootstrap self-update、runner 重建、observability apply、
   prod apply。打临时槽的 canary 与预览重部署不属此类。
   **(b) 改动"决定合流的东西"本身**——门禁读工作树里的规则，AI 合流自己的 PR 时那就是该 PR
   的分支，于是改动由它自己引入的版本审判。这一类与可逆性无关，是自我裁决问题。
   受保护文件改为引用 owner 指示即可，不再单独要求二次批准。

## 🛡️ 安全与红线

- **严禁**提交任何敏感文件（`*.pem`、`.env`、`*.tfvars`）。
- **状态不一致**：Apply 冲突时必须执行 [State Discrepancy Protocol](docs/ssot/ops.standards.md#rule-4-状态不一致协议-state-discrepancy-protocol)。
- **密钥源头**：1Password 是静态密钥的唯一真源。
- **0 宕机**：有宕机风险必须主动提出；必须宕机时须给出降低时长的方案。

## 📎 按需加载

导航索引只维护一份，在 [`docs/README.md`](docs/README.md)——本文不再复制目录树。

| 要做什么 | 读哪里 |
|---|---|
| 提 PR / 判断能否合流 / 执行 merge | [`docs/ssot/ops.merge-gate.md`](docs/ssot/ops.merge-gate.md) |
| 写代码 / 写文档 / 用 STAR 拆任务 / 运营准则 | [`docs/ssot/core.engineering.md`](docs/ssot/core.engineering.md) |
| 查技术真理、架构、SOP | [`docs/ssot/README.md`](docs/ssot/README.md)（由 `MANIFEST.yaml` 生成） |
| 找当前任务 | [`docs/project/README.md`](docs/project/README.md) |
| 接入应用 / 新手上手 | [`docs/onboarding/README.md`](docs/onboarding/README.md) |
| 改某一层基础设施 | 该层 `README.md`（[bootstrap](bootstrap/README.md) / [platform](platform/README.md) / [tools](tools/README.md) / [libs](libs/README.md)） |

`CLAUDE.md` 是本文的软链，两个文件名指向同一份内容——Claude Code 不读裸 `AGENTS.md`，
Pi/Codex 读，这对软链是承重件，由 `ws-agents-lint` 看守。
