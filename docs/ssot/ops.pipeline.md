# Pipeline SSOT (运维流水线)

> **SSOT Key**: `ops.pipeline`
> **核心定义**: 6-Actions 架构 - 原子操作清晰，逻辑分组合理。

---

## 1. 真理来源 (The Source)

本话题的配置和状态由以下物理位置唯一确定：

| 维度 | 物理位置 (SSOT) | 说明 |
|------|----------------|------|
| **CI 定义** | [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) | 6 个 actions 定义 |
| **Bootstrap 脚本** | [`tools/ci/bootstrap.py`](../../tools/ci/bootstrap.py) | L1 层管理 |
| **Digger 配置** | [`digger.yml`](../../digger.yml) | Projects 定义、OSS 配置 |
| **测试防护** | [`tests/conftest.py`](../../tests/conftest.py) | Precondition checks |

---

## 2. 架构模型 (6-Actions)

### 核心设计

**10 个原子操作** → **6 个逻辑 Action**

| 原子操作 | 所属 Action |
|---------|------------|
| TF fmt | check |
| TF validate | check |
| Digger fmt | check |
| Digger validate | check |
| Bootstrap plan | bootstrap-plan |
| TF plan | plan |
| Digger plan | plan |
| Bootstrap apply | bootstrap-apply |
| TF apply | apply |
| Digger apply | apply |
| E2E tests | e2e |

### 6 个 Actions 定义

#### 1. check
**职责**: 格式和配置检查  
**包含**: TF fmt + TF validate + Digger fmt + Digger validate  
**触发**:
- Auto: PR push, post-merge
- Manual: `/check`

#### 2. bootstrap-plan
**职责**: L1 层计划  
**包含**: Bootstrap plan  
**触发**:
- Auto: PR push, post-merge
- Manual: `/bootstrap-plan`
- **依赖**: check (仅 post-merge)

#### 3. plan
**职责**: L2-L4 层计划  
**包含**: TF plan + Digger plan  
**触发**:
- Auto: PR push, post-merge
- Manual: `/plan`
- **依赖**: check (仅 post-merge)

#### 4. bootstrap-apply
**职责**: L1 层部署  
**包含**: Bootstrap apply  
**触发**:
- Auto: post-merge only
- Manual: `/bootstrap-apply`
- **依赖**: check, bootstrap-plan, plan

#### 5. apply
**职责**: L2-L4 层部署  
**包含**: TF apply + Digger apply  
**触发**:
- Auto: post-merge only
- Manual: `/apply`
- **依赖**: check, bootstrap-plan, plan

#### 6. e2e
**职责**: 端到端测试  
**包含**: E2E test suite  
**触发**:
- Auto: post-merge only
- Manual: `/e2e`
- **依赖**: post-merge 依赖 bootstrap-apply, apply；手动 `/e2e` 不依赖

---

## 3. Workflow 流程图

```mermaid
graph TD
    PR[PR Push] --> Check[check]
    PR --> BPlan[bootstrap-plan]
    PR --> Plan[plan]
    
    Merge[Post-merge] --> Check2[check]
    Check2 --> BPlan2[bootstrap-plan]
    Check2 --> Plan2[plan]
    BPlan2 --> BApply[bootstrap-apply]
    Plan2 --> Apply[apply]
    BApply --> E2E[e2e]
    Apply --> E2E
```

**PR 阶段** (3个自动):
```
check → bootstrap-plan → plan
```

**Post-merge** (6个顺序):
```
check
  ├→ bootstrap-plan → bootstrap-apply →
  └→ plan → apply →
                  └→ e2e
```

---

## 4. 指令回执 (Command Feedback)

对于 `/check`、`/plan`、`/apply`、`/bootstrap-*`、`/e2e`：
- **触发即回评**：立即生成一条 “running” 评论，附带 run 链接。
- **结果回写**：CI 结束后更新同一条评论为 success/failure。

---

## 5. 事件与 Token 映射

| Event | Job | 执行 | 交互 | CI Check |
|-------|-----|------|------|----------|
| `pull_request` | `terraform-plan` | `GITHUB_TOKEN` | `infra-flash` | ✅ |
| `push` (main) | `terraform-apply` | `GITHUB_TOKEN` | - | ✅ |
| `/plan` comment | `digger` | `infra-flash` | `infra-flash` | ❌ |
| `/apply` comment | `digger` | `infra-flash` | `infra-flash` | ❌ |
| `digger -p xxx` | `digger` | `infra-flash` | `infra-flash` | ❌ |
| `/bootstrap` | `bootstrap` | `GITHUB_TOKEN` | `infra-flash` | ❌ |
| `/e2e` | `e2e-command` | `infra-flash` | `infra-flash` | ❌ |

### Token 选择逻辑

```
需要显示在 CI checks？
  ├─ Yes → GITHUB_TOKEN 执行
  │        infra-flash 发布结果
  │        (terraform-plan, terraform-apply)
  │
  └─ No → infra-flash 全流程
           (digger, bootstrap, e2e)
```

---

## 6. 设计约束 (Dos & Don'ts)

### ✅ 推荐模式 (Whitelist)

- **模式 A**: PR 创建后自动 plan，review 输出，merge 后自动 apply
- **模式 B**: 需要单独 apply 某项目时，用 `digger apply -p <project>`
- **模式 C**: Bootstrap 变更通过 `/bootstrap apply`
- **模式 D**: 重大变更前先 `/e2e` 验证

### ⛔ 禁止模式 (Blacklist)

- **反模式 A**: **禁止** 在本地执行 `terraform apply` 更新 L2+ 资源
- **反模式 B**: **禁止** 绕过 CI 直接修改线上资源
- **反模式 C**: **禁止** 直接 push 到 main (repository rule 保护)

---

## 7. 标准操作程序 (Playbooks)

### SOP-001: 部署变更 (Standard GitOps)

- **触发条件**: 代码合并前
- **步骤**:
    1. 提交代码，等待 CI 初始化 Dashboard。
    2. 评论 `/plan`，检查 Digger 输出和 Dashboard 状态。
    3. 评论 `/apply`，等待部署成功，Dashboard 显示 ✅。
    4. 合并 PR。

### SOP-002: 触发 E2E 测试

- **触发条件**: 需要验证部署效果
- **步骤**:
    1. 评论 `/e2e` (运行所有 smoke tests)。
    2. 或评论 `/e2e full` (运行完整回归测试)。
    3. 查看 Dashboard 链接的测试报告。

### SOP-003: 紧急回滚

- **触发条件**: 部署导致故障
- **步骤**:
    1. `git revert <commit-id>`。
    2. 提交新 PR。
    3. 快速执行 `/apply` (可跳过详细 Plan 审查)。

### SOP-004: AI Code Review (可选)

- **触发条件**: 需要 AI 辅助审查代码变更
- **步骤**:
    1. 在 PR 评论中输入 `@copilot please review` 或 `/review`。
    2. 等待 GitHub Copilot 分析并回复。
    3. 根据建议修改代码或标记为已阅。
- **注意**:
    - **手动触发**: 不会自动运行，需主动请求。
    - **权限要求**: 需要 Copilot 订阅或组织授权。
    - **Dashboard**: Copilot 原生 review 不会更新 PR Dashboard。
    - **详细指南**: 参见 [`docs/project/active/ai_code_review.md`](../project/active/ai_code_review.md)。

### SOP-005: Dashboard 故障排查

- **症状**: Dashboard 不更新，所有状态 ⏳ pending
- **排查步骤**:
    1. 检查 workflow logs: `gh run view <run-id> --log | grep -i dashboard`
    2. 验证 PR 评论中是否有 marker: `<!-- infra-dashboard:{sha} -->`
    3. 检查 GitHub Token 权限（需要 `issues: write`）
    4. 手动测试：`python -m ci update --pr <num> --stage apply --status success`
- **常见原因**: 参见下方 SOP-006

### SOP-006: 通用故障排查 (General Troubleshooting)

#### 1. Commands not responding (指令无响应)
- **Check**:
    1. Workflow `if` conditions in `ci.yml`.
    2. Comment spelling (case-sensitive, e.g., `/plan`).
    3. Context: PR comment vs Issue comment (must be PR).

#### 2. Bootstrap fails (L1 失败)
- **Check**:
    1. `tools/ci/bootstrap.py` exists and is executable.
    2. Terragrunt/Terraform setup action works.
    3. Bootstrap directory exists.

#### 3. Digger not planning all projects (Plan 不全)
- **Check**:
    1. `digger.yml` has `include_patterns: ["**/*"]` for drift detection.
    2. Projects are properly configured in `digger.yml`.

#### 4. No comments appearing (无评论回显)
- **Check**:
    1. GitHub App token has correct permissions.
    2. Workflow has `pull-requests: write` permission.
    3. Job completed successfully (check Actions logs).

---

## 8. 验证与测试 (The Proof)

| 行为描述 | 测试文件 (Test Anchor) | 覆盖率 |
|----------|-----------------------|--------|
| **Dashboard 创建与更新** | Manual test: `python -m ci init/update` | ⚠️ Manual |
| **Pipeline 逻辑验证** | [`tools/ci/tests/test_pipeline_parser.py`](../../tools/ci/tests/test_pipeline_parser.py) | ✅ Unit Test |
| **Digger 集成验证** | [`e2e_regressions/tests/bootstrap/compute/test_digger.py`](../../e2e_regressions/tests/bootstrap/compute/test_digger.py) | ⏳ Pending |

---

## Used by

- [docs/ssot/README.md](./README.md)
- [bootstrap/README.md](../../bootstrap/README.md)
