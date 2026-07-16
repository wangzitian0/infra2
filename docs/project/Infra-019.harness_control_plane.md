# Infra-019: Workspace Harness Control Plane

**Status**: In Progress
**Owner**: Infra2 maintainers
**SSOT**: [`core.harness`](../ssot/core.harness.md), [`core`](../ssot/core.md)

## Goal

把当前仓库确立为以 `infra2`、`infra2-sdk` 和 workspace 通用偏好为重点的可验证
harness，提供独立的 TUI tooling sub-repo，同时保持 Finance Report 与 TrueAlpha 的
自主迭代和独立发布。

## Situation / Step 0

Infra-018 已消除 App 对 infra2 源码的递归依赖，并确认 submodule 只表示开发快照。
当前缺口是 workspace 角色仍只存在于人类描述中，根 agent 规则也没有显式声明进入
App 后的治理优先级。两个 App 已形成各自成熟但不同的工作流，复制或统一这些规则会
重新制造耦合。

## Tasks

1. **Workspace**: 建立机器可读仓库清单，区分 focus 与 autonomous checkout。
2. **Workspace**: 提炼 GitHub、协作和软件设计偏好，但不向 App 分发 policy。
3. **Infra2**: 提供只读 `harness check` 和回归测试，fail closed 校验自治边界。
4. **Documentation**: 更新根入口、SSOT、Project 和目录 README。
5. **Workspace tooling**: 以 root-level submodule 接入 `oh-my-code-agent`，后续承载
   TUI 管理，但不进入 infra/App runtime dependency graph。

## Design Decisions

- Harness focus 仅为 `infra2` 与 `infra2-sdk`。
- App 出现在清单中是为了 workspace 可见性，不表示 policy ownership。
- Workspace 偏好在目标仓库本地规则存在时自动让位。
- SDK 只承载版本化机器契约，不承载 GitHub 或 agent 行为偏好。
- 第一阶段校验完全只读，不自动更新 submodule 或生成 App 变更。
- Workspace tooling 使用独立 release 或 pinned commit，不与 infra tag/SDK SemVer 混用。

## Verification (The Proof)

```bash
uv run pytest -q libs/tests/test_harness_manifest.py
uv run python -m tools.harness check --json
uv run pytest -q libs/tests/test_sdk_contract_adoption.py
mkdocs build --config-file docs/mkdocs.yml
```

## Rollback

删除 `harness/`、校验器与对应索引变更即可回滚。该阶段没有 runtime、deployment、
secret 或 App source mutation，不产生线上 drift。

## Change Log

| Date | Change |
|---|---|
| 2026-07-16 | Phase 1: repository inventory, workspace preferences, read-only checker, and autonomy boundary |
| 2026-07-16 | Phase 2: add root-level `oh-my-code-agent` submodule for future TUI management |
| 2026-07-16 | Phase 2 follow-up: pin `oh-my-code-agent` to its design-complete main head and unify the submodule URL scheme to HTTPS |

## TODOWRITE

See [`Infra-019.TODOWRITE.md`](./Infra-019.TODOWRITE.md).
