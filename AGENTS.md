# Infra2 Harness 与基础设施 AI Agent 行为准则

> **权限边界**：AI 修改本文件需 owner 明确指示，并在 PR description 中引用那句指示。AI 可在"合流门禁"全部满足后自行 Merge PR（常设合流权是 workspace 级事实，见工作区根 `AGENTS.md`「合流授权」，本文不重述；本仓库的门禁条件见 [`docs/ssot/ops.merge-gate.md`](docs/ssot/ops.merge-gate.md)）；任一状态失败、缺失或无法验证时必须 fail-closed，禁止合流。

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
   不复制、不分发、不强制同步 App policy。**但合流授权不是 App policy**——它是 owner 对
   自己名下仓库的一次性授权，不因换了仓库而失效，不要进了 App 就重新推导出"需要单独批准"。
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
3. **Review 已闭环**：未 resolved 发现按 severity 加权（high=1.0 / middle=0.5 / low=0.25，未标注按 middle），**总分 ≥ 1.0 即禁止合流**。由 `pr_merge_gate` 计分，不靠人心算；`severity: <级别>` 是唯一被识别的标注形式，从散文措辞推断等级会让判决取决于句子怎么写。
4. **绿是当前的，且必需检查确实报告过**：兄弟 PR 合入后改写了本 PR 也动的文件时，检查仍是绿的
   却没重跑；`blocks_merge` 的检查被 skip 时 GitHub 也接受为已满足。判定统一走
   `python -m tools.pr_merge_gate <n> --policy either --request-review --merge`
   （exit 1 = 未到时机，exit 2 = 需要 owner），不靠肉眼看表。
5. **按环境划线（对全部仓库一致）**：合流触发 **staging** 部署、临时槽 canary、预览重部署
   ——AI 可自行合流。**owner 按权限保留的只有 prod 部署这一类**，必须回到 owner 并批准当前
   `head SHA`：prod apply / promote、L1 bootstrap self-update、runner 重建、observability apply。
   还有一类回到 owner 的**不是权限问题，是自我裁决问题**——**改动"决定合流的东西"本身**：
   门禁从工作树读规则，AI 合流自己的 PR 时读到的就是该 PR 引入的版本，等于由被告改写的法条
   来审判。**判据不是一份文件清单，而是可计算的依赖闭包**：`pr_merge_gate` 自身、它 import 的
   一切、它读的数据文件，以及这些文件各自的测试——由 `tools/pr_merge_gate.self_governing_files()`
   算出，**新增一个 import 自动纳入保护，不靠谁记得**。只有代码读不到的两份**规则文本**
   （本文件与 `ops.merge-gate.md`）是显式列举的，因为闭包到不了它们。
6. **自我裁决按方向裁，不按文件裁**（2026-09-22 owner 指示）：被告改写法条的危害是单向的
   ——**对自己有利**。一个只会让门禁更常说「不」的改动不可能对自己有利，所以**方向可由代码
   机械证明为收紧的，门禁自行放行；放松、或证明不出来的，回到 owner 审核**。
   **证明必须算出来，不能在 PR 描述里声称**：能证明的只有闭集——有 schema、且只喂给一个判定
   的数据文件（`ci-gate-inventory.yaml` 的判定输入是「哪些 check 必须绿」，base 与 head 做
   集合比较即可）。Python 与规则散文没有这种读法，任何一行都可能放松任何东西，一律回 owner。
   未知文件、读不到、解析失败、base 侧为空——全部算证明不出来。**默认是回 owner。**
   受保护文件引用 owner 指示即可。

## 🛡️ 安全与红线

- **严禁**提交任何敏感文件（`*.pem`、`.env`、`*.tfvars`）。
- **状态不一致**：Apply 冲突时必须执行 [State Discrepancy Protocol](docs/ssot/ops.standards.md#rule-4-状态不一致协议-state-discrepancy-protocol)。
- **密钥源头**：1Password 是静态密钥的唯一真源。
- **0 宕机**：有宕机风险必须主动提出；必须宕机时须给出降低时长的方案。

## 📎 按需加载

导航索引只维护一份，在 [`docs/README.md`](docs/README.md)——本文不再复制目录树。

| 要做什么 | 读哪里 |
|---|---|
| 全局工程概览 / 快速开始 | [`README.md`](README.md) |
| 提 PR / 判断能否合流 / 执行 merge | [`docs/ssot/ops.merge-gate.md`](docs/ssot/ops.merge-gate.md) |
| 写代码 / 写文档 / 用 STAR 拆任务 / 运营准则 | [`docs/ssot/core.engineering.md`](docs/ssot/core.engineering.md) |
| 查技术真理、架构、SOP | [`docs/ssot/README.md`](docs/ssot/README.md)（由 `MANIFEST.yaml` 生成） |
| 找当前任务 | [`docs/project/README.md`](docs/project/README.md) |
| 接入应用 / 新手上手 | [`docs/onboarding/README.md`](docs/onboarding/README.md) |
| 改某一层基础设施 | 该层 `README.md`（[bootstrap](bootstrap/README.md) / [platform](platform/README.md) / [tools](tools/README.md) / [libs](libs/README.md)） |

`CLAUDE.md` 与本文同内容：它是一条**入库的软链** → `AGENTS.md`。**真源只有 `AGENTS.md`，
改规则改这里。** Claude Code 自 v2.1.277 起也能直接读 `AGENTS.md`，但工作目录或任一祖先目录
存在 `CLAUDE.md` 时它**只读** `CLAUDE.md`，而这个 workspace 的父目录里就有一个——所以本仓库
必须自带载体，不能靠原生支持。

载体形态是**测出来的，不是选出来的**（#856）。判别式探针问「已加载的指令文本里有没有这个字符串」
并禁止读文件，每格两次：

| 载体 | 仓库根 | 子目录 |
|---|---|---|
| 单行 `@AGENTS.md` 导入 | 读到 | **读不到** |
| 入库软链 → `AGENTS.md` | 读到 | 读到 |
| 仅 `AGENTS.md`（祖先有 `CLAUDE.md`） | **读不到** | **读不到** |

**`@import` 只在 `CLAUDE.md` 位于当前工作目录时解析**；被祖先遍历找到时不解析。于是从
`libs/`、`tools/`、任何 worktree 子目录起的会话，全程没有合流门禁、没有 SSOT First、没有红线，
而且没有任何信号。软链换来的代价是 Windows 无 `core.symlinks` 时 checkout 会把它落成一行纯文本
——**这个风险仍然存在，但它被改成会响**：`libs/tests/test_claude_md_carrier.py` 里
`test_a_broken_symlink_checkout_fails_loudly` 按内容形态判定，任何平台都能照出来。
