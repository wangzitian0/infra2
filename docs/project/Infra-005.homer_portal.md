# Infra-005: Homer Portal + SSO Protection

**Status**: Completed  
**Owner**: Infra  
**Priority**: P2  

## Goal
`home.zitian.party` 可访问，且通过 Authentik SSO 保护，只有 `admins` 组用户能访问。

## Context
统一入口页 + SSO 访问控制，保证安全性和可复现性。

## Scope
- [x] Add Homer portal service under `platform/21.portal`.
- [x] Render config template and document the update flow.
- [x] Automate Cloudflare DNS records for `cloud/op/vault/sso/home`.
- [x] Automate Cloudflare SSL settings and HTTPS warm-up.
- [x] Add SSOT for DNS/cert automation and update Bootstrap docs.
- [x] Configure Traefik forward auth to Authentik.
- [x] Create Authentik Root Token and store in Vault.
- [x] Setup admin group and add akadmin.
- [x] Create Portal SSO application with group-based access.
- [x] Verify end-to-end: unauthenticated → login → access.
- [x] Fix: Disable Dokploy auto-domain for SSO-protected services.
- [x] Add logout link to Homer config.

## Deliverables
- Docker Compose + deploy tasks for portal
- Homer config template and generated config path
- Platform README updates
- Cloudflare DNS automation tasks + .env.example
- DNS/TLS SSOT + Bootstrap README updates
- **Authentik SSO integration with group-based access control**
- **SSO SSOT documentation**

## PR Links
- PR #28: https://github.com/wangzitian0/infra2/pull/28

## Change Log
| Date | Change |
|------|--------|
| 2025-12-30 | Initialized project |
| 2025-12-31 | Added Traefik forward auth labels |
| 2025-12-31 | Created Authentik shared tasks for SSO automation |
| 2025-12-31 | Added group-based access control |
| 2026-01-01 | Fixed SSO by disabling Dokploy auto-domain |
| 2026-01-01 | Added logout link to Homer |

## Verification
- [x] `invoke portal.shared.status`
- [x] `https://home.${INTERNAL_DOMAIN}` loads portal (without SSO)
- [x] `invoke authentik.shared.create-root-token` succeeds
- [x] `invoke authentik.shared.setup-admin-group` succeeds
- [x] `invoke authentik.shared.create-proxy-app --name=Portal ...` succeeds
- [x] Unauthenticated access → redirects to login
- [x] Non-admin user → access denied (policy configured)
- [x] Admin user → portal loads

## TODOWRITE

- [Infra-005.TODOWRITE.md](./Infra-005.TODOWRITE.md)

## Key Learnings

### SSO 与 Dokploy 域名配置冲突

**问题**：Portal 可以在隐身模式访问，SSO 认证没有生效。

**根本原因**：Dokploy 通过 API 自动配置域名时，会生成额外的 Traefik router（如 `platform-portal-yl8mdl-5-websecure`），这个 router **没有** forwardauth 中间件，导致绕过认证。

**解决方案**：
1. 在 `deploy.py` 中设置 `subdomain = None` 禁用 Dokploy 自动域名配置
2. 完全依赖 `compose.yaml` 中的 Traefik labels
3. 如果已有 Dokploy 域名配置，需要从数据库删除

**规则**：SSO 保护的服务必须禁用 Dokploy 自动域名配置。

## Open Issues
- [x] Deploy the portal app in Dokploy and confirm `https://home.${INTERNAL_DOMAIN}`.
- [ ] Confirm `bootstrap/cloudflare` item fields are complete.
- [ ] Dokploy Server Domain SSL is still manual.
- [x] **Run SSO setup tasks and verify access control.**

## References
- [SSOT: bootstrap.dns_and_cert](../ssot/bootstrap.dns_and_cert.md)
- [SSOT: platform.sso](../ssot/platform.sso.md) *(new)*
- [Portal README](../../platform/21.portal/README.md)
- [Platform portal compose](../../platform/21.portal/compose.yaml)
- [Authentik shared tasks](../../platform/10.authentik/shared_tasks.py)
