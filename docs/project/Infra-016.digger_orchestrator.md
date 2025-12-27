# Infra-016: Digger Orchestrator 自部署

**Status**: Proposed  
**Owner**: Infra  
**Priority**: P1

## Goal

自部署 Digger Orchestrator 实现 post-merge 自动 apply，统一 L1-L3 层 CI/CD 编排。

## Context

### 当前痛点
1. **Digger OSS 限制**: 不支持 `push` 事件，无法在 post-merge 时自动 apply
2. **Workaround 复杂**: 需要维护两套逻辑 (PR: Digger, Push: 原生 terragrunt)
3. **功能缺失**: OSS 版本无法使用 `on_commit_to_default: [digger apply]`

### 自部署优势
- ✅ 完整事件支持: 支持 `push` 事件，可监听 webhook 并触发 CI
- ✅ 统一编排: 所有层通过 Digger 管理，不需要特殊处理
- ✅ K8s 内运行: 完整网络访问 Vault/Kubernetes API

## Scope

### Phase 1: 基础设施准备 (1 天)
- 在 `bootstrap/2.digger.tf` 创建 Helm release
- 复用 Platform PG (`digger` database)
- Ingress 配置: `digger.${INTERNAL_DOMAIN}`

### Phase 2: GitHub App 配置 (0.5 天)
- 复用 `infra-flash` GitHub App
- 配置 Webhook URL

### Phase 3: 部署与验证 (1 天)
- Apply bootstrap 变更
- 测试 PR plan / post-merge apply

### Phase 4: CI 简化 (0.5 天)
- 移除 bootstrap 专用 jobs
- 更新 `digger.yml` 配置

## Dependencies

- Platform PostgreSQL available
- `infra-flash` GitHub App configured
- Wildcard TLS certificate

## PR Links

- TBD

## Change Log

- 2025-12-26: Initial planning (folded into project change log)

## Verification

- PR 推送后自动触发 plan
- Merge 到 main 后自动触发 apply
- `/apply` 注释仍可手动触发

## SSOT Reference

- [ops.pipeline.md](../ssot/ops.pipeline.md)
