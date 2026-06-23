# Infra-008: Platform Multi-Environment (Staging)

**Status**: In Progress  
**Owner**: Infra  
**Priority**: P1

> **契约归属 (SSOT)**: 多环境的**触发 / 发布**模型由 [docs/ssot/ops.pipeline.md](../ssot/ops.pipeline.md) 拥有,
> 环境**定义**由 [core.environments.md](../ssot/core.environments.md) 拥有。本 EPIC 只追踪交付,不重述契约。

## Goal
Platform 支持 staging 与 production 并行，优先使用 Dokploy 的 Project/Environment/Service 分层，环境差异通过环境变量配置。

## Context
当前 platform 只有 production 环境，改动时容易影响线上。Dokploy 原生支持多环境，需要将部署和配置体系升级为“一份代码，多环境配置”，并保证 staging/production 可同时运行。

## Scope
- [x] 增加 Dokploy 环境选择（按 DEPLOY_ENV）
- [x] 提供 Dokploy Environment 创建命令（CLI）
- [ ] 环境差异通过 Dokploy Environment 变量配置（DATA_PATH / ENV_SUFFIX 可选）
- [ ] 仅在必须手写 label 的场景使用后缀（Traefik router/service 名避免冲突）
- [ ] 域名统一为 `{subdomain}{ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}`（prod 为空，env 内部用 `_`）
- [ ] SSOT/README 更新（平台环境策略）

## Deliverables
- 环境感知的部署逻辑（ENV 选择 + Environment 变量注入）
- DATA_PATH（必要时 ENV_SUFFIX）由 Dokploy Environment 管理
- 域名规则 `{subdomain}{ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}` 落地
- 平台服务 staging/production 可并行运行
- SSOT + Platform 文档更新

## PR Links
- https://github.com/wangzitian0/infra2/pull/42 (WIP)

## Change Log
| Date | Change |
|------|--------|
| 2026-01-02 | Initialized project |
| 2026-01-02 | Implemented multi-env platform deployment + docs |
| 2026-01-03 | Added Dokploy environment CLI helper |
| 2026-01-03 | Aligned ENV_DOMAIN_SUFFIX usage + portal Traefik labels |

## Verification
- [ ] `python -m tools.deploy_v2 --service platform/postgres --type staging --iac-ref vX.Y.Z --domain zitian.party` 成功部署
- [ ] `python -m tools.deploy_v2 --service platform/redis --type staging --iac-ref vX.Y.Z --domain zitian.party` 成功部署
- [ ] `python -m tools.deploy_v2 --service platform/authentik --type staging --iac-ref vX.Y.Z --domain zitian.party` 域名使用 `sso-staging.${INTERNAL_DOMAIN}`
- [ ] `DEPLOY_ENV=production invoke portal.shared.status` 生产不受影响

## References
- [SSOT: core](../ssot/core.md)
- [SSOT: platform.automation](../ssot/platform.automation.md)
- [SSOT: platform.domain](../ssot/platform.domain.md)
- [SSOT: bootstrap.vars_and_secrets](../ssot/bootstrap.vars_and_secrets.md)
