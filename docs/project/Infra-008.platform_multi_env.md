# Infra-008: Platform Multi-Environment (Staging)

**Status**: In Progress  
**Owner**: Infra  
**Priority**: P1

## Goal
Platform supports staging alongside production with env-scoped domains, containers, data paths, and Dokploy environment selection.

## Context
当前 platform 只有 production 环境，改动时容易影响线上。Dokploy 原生支持多环境，需要将部署和配置体系升级为“一份代码，多环境配置”，并保证 staging/production 可同时运行。

## Scope
- [x] 增加 Dokploy 环境选择（按 DEPLOY_ENV）
- [x] 容器命名、数据路径、域名按环境隔离
- [x] 平台服务 compose 与 deployer 适配 ENV_SUFFIX/DATA_PATH
- [x] SSOT/README 更新（平台环境策略）

## Deliverables
- 环境感知的部署逻辑（ENV/ENV_SUFFIX/DATA_PATH）
- 平台服务 staging/production 可并行运行
- SSOT + Platform 文档更新

## PR Links
- None yet.

## Change Log
| Date | Change |
|------|--------|
| 2026-01-02 | Initialized project |
| 2026-01-02 | Implemented multi-env platform deployment + docs |

## Verification
- [ ] `DEPLOY_ENV=staging invoke postgres.setup` 成功部署
- [ ] `DEPLOY_ENV=staging invoke redis.setup` 成功部署
- [ ] `DEPLOY_ENV=staging invoke authentik.setup` 域名使用 `sso-staging`
- [ ] `DEPLOY_ENV=production invoke portal.shared.status` 生产不受影响

## References
- [SSOT: core](../ssot/core.md)
- [SSOT: platform.automation](../ssot/platform.automation.md)
- [SSOT: platform.domain](../ssot/platform.domain.md)
- [SSOT: bootstrap.vars_and_secrets](../ssot/bootstrap.vars_and_secrets.md)
