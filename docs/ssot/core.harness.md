# Workspace Harness Control Plane SSOT

> **SSOT Key**: `core.harness`
> **核心定义**: 定义统一 checkout 中的仓库角色、治理边界与 workspace 协作模型。

## 1. The Source

| 事实 | 唯一来源 |
|---|---|
| Workspace 成员、角色、治理模式 | `harness/repos.yaml` |
| Workspace 通用偏好 | `harness/workspace/` |
| Infra2 架构与运维规则 | [`docs/ssot/core.md`](./core.md) 与相关 SSOT |
| SDK 公共契约 | 已发布的 `infra2-sdk` SemVer artifact |
| App 开发与领域规则 | 各 App 自己的 `AGENTS.md`、架构文档、代码和 CI |

## 2. Goal And Non-Goals

Harness 提供一个统一入口，用于协同开发 `infra2`、`infra2-sdk`，并在需要时查看自主
App 的集成状态。它统一 workspace 视角，不统一产品仓库的迭代模型。

明确不做：

- 不从 harness 向 App vendoring、symlink 或同步 policy/skill。
- 不把 App submodule 变成 package、runtime、deployment 或 config-hash 依赖。
- 不替 App 决定领域架构、CI gate 实例、版本节奏或发布审批。
- 不把人类工作流偏好放进 `infra2-sdk` 的运行时 wire contract。

## 3. Ownership Model

| Repository | Harness role | Governance | Release identity |
|---|---|---|---|
| `infra2` | Infrastructure implementation and deployment control plane | Local | Infra2 release tag |
| `infra2-sdk` | Versioned, side-effect-free cross-repository contract | Coordinated, independently released | SDK SemVer |
| `finance_report` | Integration-visible application checkout | Autonomous | App image ref/digest |
| `truealpha` | Integration-visible application checkout | Autonomous | App image ref/digest |

`coordinated` 不表示两个 Git 仓库合成一个发布单元。Infra2 与 SDK 仍通过各自 PR、
commit、tag 和兼容性证明独立发布；harness 只拥有两者之间的协作视图与边界定义。

## 4. Rule Precedence

1. 在根目录修改 infra2/harness 时，根 `AGENTS.md`、Project 与 infra SSOT 生效。
2. 进入嵌套仓库后，先读取该仓库本地 authority；本地规则覆盖 workspace 偏好。
3. Workspace guide 只提供默认决策倾向，不得绕过目标仓库的命令、gate 或审批。
4. App 规则与 workspace 偏好冲突时，App 自治规则胜出；不通过修改 App 来消除差异。

## 5. Contract Versus Instance

- `infra2-sdk` 可以拥有稳定、无副作用的数据模型、枚举、校验与序列化。
- Infra2 和 App 各自拥有 CI gate 实例、部署实现、运行时 mutation 与人类治理规则。
- 跨仓库视图优先在读取时 join 已发布证据，不复制第二份可漂移的事实。
- 新抽象只有在语义稳定且至少有清晰的多消费者契约时才进入 SDK。

这一边界延续 [`core.md` §3.1](./core.md#31-repository-dependency-boundary) 与已归档
[`Infra-018`](../project/archive/Infra-018.repository_boundary_decoupling.md) 的源码解耦结论。

## 6. Workspace Operation

```bash
git submodule update --init --recursive
uv run python -m tools.harness check
```

`harness check` 是只读校验：验证清单 schema、focus、角色/治理组合、workspace 偏好和
authority 路径。未初始化的 checkout 只报告 warning；结构错误、App 非自治或 authority
漂移会失败。命令不得执行 fetch、checkout、写文件、发布或部署。

## 7. The Proof

```bash
uv run pytest -q libs/tests/test_harness_manifest.py
uv run python -m tools.harness check --json
uv run pytest -q libs/tests/test_sdk_contract_adoption.py
```

证明标准：

- 清单只把 `infra2` 和 `infra2-sdk` 列为 focus。
- 所有 `external-application` 必须是 `autonomous`，且不能进入 focus。
- authority 与 preference 路径可读且不能逃逸 workspace root。
- 现有 SDK 采用测试继续证明消费者使用发布 artifact，而不是 submodule 源码。
