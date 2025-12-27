# Infra-003: CI/CD Deploy Verification Architecture

**Status**: Archived  
**Owner**: Infra  
**Legacy Source**: BRN-008 (CI/CD Architecture Design)

## Summary
Define a three-layer verification model to close the gap between Terraform apply
and real deployment readiness, with PR feedback loops.

## PR Links
- PR #357: https://github.com/wangzitian0/infra/pull/357
- PR #290: https://github.com/wangzitian0/infra/pull/290
- PR #289: https://github.com/wangzitian0/infra/pull/289

## Change Log
- [2025-12-19: Pipeline V2 Upgrade](../../change_log/2025-12-19.pipeline_v2_upgrade.md)
- [2025-12-15: Infra-Flash Per-Commit](../../change_log/2025-12-15.infra_flash_per_commit.md)

## Git Commits (Backtrace)
- e52a10d feat(e2e): Add CI integration for E2E regression tests (#357)

## Legacy Design (BRN-008 CI/CD Architecture)

## 问题陈述

### 当前状态（问题）

```
Atlantis apply 成功 ✓
    ↓
CI 显示 ✅ "Deployment successful"
    ↓
但实际上：
- 资源可能还没就绪
- 数据库连接可能失败
- Portal SSO 可能没配好
    ↓
用户手动测试 ← 浪费时间，低效
```

### 核心问题

1. **Atlantis apply ≠ 部署完成**
   - apply 只表示 Terraform 语法正确、资源创建请求成功
   - 不表示 Kubernetes 资源已就绪
   - 不表示应用层面的功能正常

2. **E2E 测试与 apply 脱节**
   - 烟雾测试依赖手动触发或定时任务
   - 没有机制自动在 apply 成功后运行
   - 用户无法从 PR 上看到"部署真的成功了吗"

3. **错误反馈延迟**
   - apply 成功但资源启动失败 → 用户察觉不到
   - 需要等待定时 E2E 测试才能发现问题
   - 故障排查难度高

---

## 设计目标

✅ **clear**: 用户清楚知道部署是否真的成功
✅ **fail-fast**: 问题立即显示在 PR 评论上
✅ **no-waste**: apply 失败立即停止，不浪费资源/时间
✅ **actionable**: 错误明确，容易定位根因

---

## 建议方案

### 架构：三层验证模型

```
┌────────────────────────────────────────────────────┐
│ Layer 1: Apply Status (基础设施同步)                │
│ Atlantis apply ✓ / ✗                               │
│ Cost: ~5 min                                        │
└────────────────────────────────────────────────────┘
                       ↓
       ┌───────────────────────────────┐
       │ 成功？                         │
       └───────────────────────────────┘
              ✓          ✗
              │          └→ STOP: CI = FAIL
              │
┌─────────────────────────────────────────────────────┐
│ Layer 2: Resource Readiness (K8s 资源就绪检查)      │
│ kubectl wait --for=condition=ready pod/...          │
│ Cost: ~5 min                                        │
└─────────────────────────────────────────────────────┘
              ✓          ✗
              │          └→ STOP: CI = FAIL
              │
┌─────────────────────────────────────────────────────┐
│ Layer 3: E2E Tests (功能验证)                        │
│ • Smoke tests (1-2 min)                             │
│ • API health (2-3 min)                              │
│ • Database connectivity (3-5 min)                   │
│ Cost: ~5-10 min                                     │
└─────────────────────────────────────────────────────┘
              ✓          ✗
              │          └→ STOP: CI = FAIL
              │
              → CI = SUCCESS
                (部署真的完成了！)
```

### CI 状态对应

| 阶段 | 结果 | PR 评论显示 | 下一步 |
|------|------|-----------|--------|
| Apply | ✓ | 🟡 Waiting for resources... | → Layer 2 |
| Apply | ✗ | ❌ Apply failed: [error] | STOP |
| Readiness | ✓ | 🟡 Running smoke tests... | → Layer 3 |
| Readiness | ✗ | ❌ Resources not ready: [pod status] | STOP |
| E2E Tests | ✓ | ✅ Deployment successful! | SUCCESS |
| E2E Tests | ✗ | ❌ E2E test failed: [test name] | STOP |

---

## 实现步骤（简化版）

### 流程
```
merge to main（Atlantis apply 已完成）
    ↓
e2e-tests.yml 自动触发
    ├─ readiness (kubectl wait)
    └─ e2e-smoke-tests (make test-smoke)
```

### GitHub Actions 工作流改造

新建 `.github/workflows/e2e-tests.yml`：

```yaml
name: E2E Tests

on:
  # merge 到 main 后自动触发（此时 Atlantis apply 已完成）
  push:
    branches: [main]
    paths:
      - '2.platform/**'      # L2 Platform
      - 'envs/*/3.data/**'   # L3 Data
      - '4.apps/**'          # L4 Apps

  # 手动触发（调试用）
  workflow_dispatch:

jobs:
  # Layer 2: 等资源启动（5 分钟）
  post-apply-readiness:
    name: Wait for Resources Ready
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - name: Configure kubectl
        run: |
          mkdir -p $HOME/.kube
          echo "${{ secrets.KUBECONFIG }}" | base64 -d > $HOME/.kube/config

      - run: kubectl wait --for=condition=ready pod -l app=vault -n platform --timeout=5m
      - run: kubectl wait --for=condition=ready pod -l app=postgres -n data-prod --timeout=5m
      - run: kubectl wait --for=condition=ready pod -l app=redis -n data-prod --timeout=5m

  # Layer 3: E2E 烟雾测试（1-2 分钟）
  e2e-smoke-tests:
    name: E2E Smoke Tests
    needs: post-apply-readiness
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'

      - run: |
          cd e2e_regressions
          uv sync
          uv run pytest -m smoke --tb=short -v

  # 最终报告
  deployment-status:
    name: Report Status
    needs: [post-apply-readiness, e2e-smoke-tests]
    if: always()
    runs-on: ubuntu-latest
    steps:
      - name: Success
        if: success()
        run: echo "✅ Deployment successful (resources ready + E2E passed)"

      - name: Failure
        if: failure()
        run: |
          echo "❌ Deployment failed"
          exit 1
```

**关键点**：
- merge = apply 已完成，无需检查
- 直接跑 readiness + e2e
- 简洁清晰

### 步骤 2: Secrets 配置

在 GitHub 仓库中添加以下 Secrets：

```
KUBECONFIG          base64 编码的 kubeconfig 文件
PORTAL_URL          https://home.example.com
VAULT_URL           https://secrets.example.com
DASHBOARD_URL       https://kdashboard.example.com
SSO_URL             https://sso.example.com
TEST_USERNAME       (可选) 用户名
TEST_PASSWORD       (可选) 密码
```

---

## 比较：当前 vs 建议

| 方面 | 当前状态 | 建议方案 |
|------|---------|--------|
| **用户反馈** | "✅ Apply OK" → 需要手动检查 | "✅ Deploy Complete" → 真的成功了 |
| **故障检测** | 定时运行，延迟大 | 立即运行，秒级反馈 |
| **重试策略** | 手动重新运行 apply | 自动等待资源就绪 |
| **错误分类** | 笼统：apply fail | 细致：apply/readiness/e2e fail |
| **资源成本** | apply 失败仍运行 E2E | apply 失败直接停止 |
| **用户体验** | 低效 | 高效 |

---

## 实施计划

### Phase 1: 框架（已完成）✅
- ✅ E2E 测试框架已创建（e2e_regressions/）
- ✅ SSOT 文档已编写（docs/ssot/ops.e2e-regressions.md）
- ✅ 各层 README 已更新

### Phase 2: CI 流程改造（待实施）
- [ ] 修改 `.github/workflows/deploy-k3s.yml`
  - 分离 apply、readiness、e2e 为三个独立的 job
  - 添加 condition 控制执行流
  - 改进错误报告

- [ ] 修改 `atlantis.yaml`
  - 确保 apply 的返回码准确反映成功/失败

- [ ] 优化 `e2e_regressions/tests/`
  - 区分可重试 vs 不可重试的错误
  - 改进错误消息

### Phase 3: 测试和优化（待实施）
- [ ] 在 staging 环境测试新流程
- [ ] 收集反馈，优化超时时间
- [ ] 建立性能基线

---

## 用户决策 ✅

### Q1: 三层模型OK吗？
**用户答**：OK，接受三层（apply → readiness → e2e）

### Q2: Readiness 等待时间？
**用户答**：（还需要决定，建议 5 分钟）

### Q3: PR 评论显示什么？
**用户答**：简介 + 错误详情链接（不要冗长列表）

**改进方案**：
```yaml
# ❌ 失败时
❌ **Deployment Failed**
- [View Apply Log](https://github.com/.../runs/123)
- [View Readiness Check](https://github.com/.../runs/123)

# ✅ 成功时
✅ **Deployment Successful**
- Vault: Ready ✓
- PostgreSQL: Ready ✓
- Redis: Ready ✓
- E2E smoke tests: Passed ✓
```

### Q4: 定时任务？
**用户答**：取消所有定时任务，全部改成 apply-trigger

**含义**：
- ❌ 删除定时烟雾测试（每 6 小时）
- ✅ 每次 apply 后自动运行完整验证
- ✅ 部署流程即时反馈，无延迟

---

## 风险评估

| 风险 | 影响 | 缓解方案 |
|------|------|--------|
| 超时时间设置过短 | 资源启动慢导致假失败 | 从宽松的超时开始，逐步调优 |
| E2E 测试本身有 bug | 错误的 fail 状态 | 先在 staging 环境充分测试 |
| PR 评论过于冗长 | 用户体验差 | 精简信息，详情链接到工作流日志 |
| 定时任务和 apply-trigger 冲突 | 重复运行、资源竞争 | 用 `if` 条件互斥 |

---

## 后续工作

### 短期（本周）
- [ ] 用户确认方案方向
- [ ] 创建 feature branch，开始改造工作流

### 中期（2 周）
- [ ] 在 staging 环境验证新流程
- [ ] 优化超时和重试策略

### 长期（持续）
- [ ] 监控 CI 成功率和平均耗时
- [ ] 定期审查和改进错误消息
- [ ] 扩展到其他部署流程（L3 等）

---

## 参考

- [E2E 回归测试 SSOT](../ssot/ops.e2e-regressions.md)
- [CI 流程文档](../ssot/ops.pipeline.md)
- [GitHub Actions 最佳实践](https://docs.github.com/en/actions/learn-github-actions)
