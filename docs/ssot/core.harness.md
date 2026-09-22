# Workspace Harness Control Plane SSOT

> **SSOT Key**: `core.harness`
> **核心定义**: 定义统一 checkout 中的仓库角色、治理边界与 workspace 协作模型。

## 1. The Source

| 事实 | 唯一来源 |
|---|---|
| Workspace 成员、角色、治理模式与契约分级 | `harness/repos.yaml` |
| Workspace 通用偏好 | `harness/workspace/` |
| Infra2 架构与运维规则 | [`docs/ssot/core.md`](./core.md) 与相关 SSOT |
| SDK 公共契约 | 已发布的 `infra2-sdk` SemVer artifact |
| Coding-agent tooling | `oh-my-code-agent` 自己的 `init.md`、`AGENTS.md`、文档地图与 release/commit |
| App 开发与领域规则 | 各 App 自己的 `AGENTS.md`、架构文档、代码和 CI |

## 2. Goal And Non-Goals

Harness 提供一个统一入口，用于协同开发 `infra2`、`infra2-sdk`，并在需要时查看自主
App 的集成状态。它统一 workspace 视角，不统一产品仓库的迭代模型。

明确不做：

- 不从 harness 向 App vendoring、symlink 或同步 policy/skill。
- 不把 App submodule 变成 package、runtime、deployment 或 config-hash 依赖。
- 不替 App 决定领域架构、CI gate 实例、版本节奏或发布审批。
- 不把人类工作流偏好放进 `infra2-sdk` 的运行时 wire contract。
- 不把 `oh-my-code-agent` 变成 infra/App 的源码或运行时依赖。

## 3. Ownership Model

| Repository | Harness role | Governance | Contract tier | Release identity |
|---|---|---|---|---|
| `infra2` | Infrastructure implementation and deployment control plane | Local | Pinned | Infra2 release tag |
| `infra2-sdk` | Versioned contracts and explicitly invoked protocol adapters | Coordinated, independently released | Pinned | SDK SemVer |
| `oh-my-code-agent` | Coding-agent observation, profiles and isolated runtimes | Coordinated, independently released | Snapshot | Tool release or pinned commit |
| `finance_report` | Integration-visible application checkout | Autonomous | Snapshot | App image ref/digest |
| `truealpha` | Integration-visible application checkout | Autonomous | Snapshot | App image ref/digest |

契约分级治理语义：
- `pinned`：pin 视为强合同。当 submodule checkout HEAD 与 parent commit pin 不一致（pin-drift）时，`harness check` 判定为 ERROR 严格阻断，`harness status` 标记为 DRIFT。
- `snapshot`：pin 仅视为开发快照记录。当 submodule 发生 HEAD 推进或漂移时，`harness check` 判定为 WARNING 记录而不阻断开发（退出码 0），`harness status` 保持 CURRENT 正常状态，确保自主 App 的自治边界与迭代节奏。

`coordinated` 不表示两个 Git 仓库合成一个发布单元。Infra2 与 SDK 仍通过各自 PR、
commit、tag 和兼容性证明独立发布；`oh-my-code-agent` 也通过自己的仓库独立演进。
Harness 只拥有这些仓库之间的协作视图与边界定义。

## 4. Rule Precedence

1. 在根目录修改 infra2/harness 时，根 `AGENTS.md`、Project 与 infra SSOT 生效。
2. 进入嵌套仓库后，先读取该仓库本地 authority；本地规则覆盖 workspace 偏好。
3. Workspace guide 只提供默认决策倾向，不得绕过目标仓库的命令、gate 或审批。
4. App 规则与 workspace 偏好冲突时，App 自治规则胜出；不通过修改 App 来消除差异。
5. `oh-my-code-agent` 的 `init.md` 定义产品边界，`AGENTS.md` 定义 agent 操作规则，
   `docs/README.md` 路由各主题 authority。其隔离能力按 host/version 的证据判断。
6. 共用工程基础指可定位的 authority、可执行的本地 proof 与独立发布契约。
   Finance Report 的正式概念归属在 `common/<pkg>/contract.py` 和 readme，
   `docs/ssot/` 已退休；TrueAlpha 以 `init.md` 和 issue acceptance 为入口。
   入口对照见 [`harness/README.md`](../../harness/README.md#repository-entry-points)。

## 5. Contract Versus Instance

- `infra2-sdk` 拥有稳定的数据模型、枚举、校验、序列化和显式调用的协议适配器。
  环境清单、密钥协议、发布身份解析以已发布 SDK 为准；import 不执行 I/O。
- Infra2 和 App 各自拥有 CI gate 实例、部署编排、适配器调用时机与人类治理规则。
- 跨仓库视图优先在读取时 join 已发布证据，不复制第二份可漂移的事实。
- 新抽象只有在语义稳定且至少有清晰的多消费者契约时才进入 SDK。
- Coding-agent 配置发现、Profile/Skill/MCP 激活和隔离 runtime 属于 `oh-my-code-agent`；
  它不承载 App 部署、通用任务调度或领域命令。App 的 Skill 内容与 policy 由 App 持有。

这一边界延续 [`core.md` §3.1](./core.md#31-repository-dependency-boundary) 与已归档
[`Infra-018`](../project/archive/Infra-018.repository_boundary_decoupling.md) 的源码解耦结论。

## 6. Workspace Operation

```bash
git submodule update --init --recursive
uv run python -m tools.harness check
uv run python -m tools.harness status --fetch
```

`harness check` 是只读校验：验证清单 schema、focus、角色/治理组合、契约分级（pinned vs snapshot）、
workspace 偏好和 authority 路径。未初始化的必需 checkout 失败；显式 optional 或 CI 不拉 submodule 时
报告 warning。结构错误、App 非自治、authority 漂移或 pinned 仓库发生 pin drift 会失败（exit 1）；
snapshot 仓库发生 HEAD 推进仅作为 warning 记录，保持 check 通过（exit 0）。命令不得执行 fetch、checkout、写文件、发布或部署。

`harness status` 只观测 checkout：显示 root/submodule parent pin、checkout HEAD、跟踪的
remote HEAD、ahead/behind、dirty path 数和 checkout release identity。默认完全本地；显式
`--fetch` 只刷新每个仓库的 `origin` refs/tags，不 checkout、pull、更改 parent pin 或触碰 App
source。`--require-current` 在任一 checkout 落后/领先/脏、或 pinned checkout 脱离 parent pin 时返回非零（snapshot 仓库的 pin drift 不会破坏 current 判定）。
观察与 fetch 前必须确认路径是独立 Git root；空 submodule 不能回退成父仓库。

`harness sweep <watch.json>` 是 workspace orchestrator（启动 subagent、盯 PR/发布/CI、
合流的主会话）的时钟，规则见
[`coordination.md` Orchestrator Liveness](../../harness/workspace/coordination.md#orchestrator-liveness)。
它把 watch list 中每个 agent、PR、release log、workflow run、worktree 归为唯一状态
`WAITING / DONE / ACTION / STALL / UNKNOWN`：只有 `WAITING` 允许继续等待，且只由 GitHub
事实白名单判定；合流 gate 只按退出码判定（0 ready、2 owner），其文本一律丢弃；携带
`--merge`、`--request-review`、`--admin`、`--auto`（含 argparse 可展开的缩写）的 gate
命令被拒绝。agent transcript 只 `stat`，从不读取。`--watch` 只打印状态迁移与心跳，
任一项离开 `WAITING` 即退出。退出码：0 无需处理、1 需行动、2 有项完成而其余仍在等待
（仅 watch）、3 停滞、4 无法判定（含用法与 watch list 错误）、5 watch 预算耗尽。
它只打印、不投递告警，因此不注册 signal（`tools/no_new_wheels_lint.py` 只约束告警投递调用）。

`tools/orchestrator_guard_hook.py` 是可选的 Claude Code hook：主会话中超过 4 分钟的
前台调用与前台等待循环被拒绝，watch list 非空却未 arm watch 时拦截一次结束回合；
subagent（hook 输入含 `agent_id`）豁免。`.claude/settings.json` 需 owner 审阅，仓库不自动接线。

## 7. The Proof

```bash
uv run pytest -q libs/tests/test_harness_manifest.py
uv run pytest -q libs/tests/test_harness_status.py
uv run pytest -q libs/tests/test_harness_sweep.py libs/tests/test_orchestrator_guard_hook.py
uv run python -m tools.harness sweep /abs/path/to/watch.json
uv run python -m tools.harness check --json
uv run python -m tools.harness status --fetch --json
uv run pytest -q libs/tests/test_sdk_contract_adoption.py
```

证明标准：

- 清单只把 `infra2` 和 `infra2-sdk` 列为 focus。
- `oh-my-code-agent` 以 root-level `workspace-tooling / coordinated` submodule 存在。
- 所有 `external-application` 必须是 `autonomous`，且不能进入 focus。
- authority 与 preference 路径可读且不能逃逸 workspace root。
- 现有 SDK 采用测试继续证明消费者使用发布 artifact，而不是 submodule 源码。
- `sweep` 对未 resolved review thread、红色 check、未知值与失败的 probe 从不报告
  `WAITING`；gate 文本不参与判定；带变更 flag 的 gate 命令在执行前被拒绝。

独立可用性证明必须经过分发边界：SDK 使用仓库外环境安装的发布 artifact；OMCA
使用脱离构建源码目录的已安装二进制，并按实际 host/version 验证。源码目录内的
测试不能替代这项证明；安全自动观测也不能替代人类 TUI/restart/model 验收。
