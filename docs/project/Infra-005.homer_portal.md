# Infra-005: Homer Portal + SSO Protection

**Status**: In Progress  
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
- [ ] Create Authentik Root Token and store in Vault.
- [ ] Setup admin group and add akadmin.
- [ ] Create Portal SSO application with group-based access.
- [ ] Verify end-to-end: unauthenticated → login → access.

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

## Verification
- [x] `invoke portal.shared.status`
- [x] `https://home.${INTERNAL_DOMAIN}` loads portal (without SSO)
- [ ] `invoke authentik.shared.create-root-token` succeeds
- [ ] `invoke authentik.shared.setup-admin-group` succeeds
- [ ] `invoke authentik.shared.create-proxy-app --name=Portal ...` succeeds
- [ ] Unauthenticated access → redirects to login
- [ ] Non-admin user → access denied
- [ ] Admin user → portal loads

## TODOWRITE

- [Infra-005.TODOWRITE.md](./Infra-005.TODOWRITE.md)

## Open Issues
- [x] Deploy the portal app in Dokploy and confirm `https://home.${INTERNAL_DOMAIN}`.
- [ ] Confirm `bootstrap/cloudflare` item fields are complete.
- [ ] Dokploy Server Domain SSL is still manual.
- [ ] **Run SSO setup tasks and verify access control.**

## References
- [SSOT: bootstrap.dns_and_cert](../ssot/bootstrap.dns_and_cert.md)
- [SSOT: platform.sso](../ssot/platform.sso.md) *(new)*
- [Portal README](../../platform/21.portal/README.md)
- [Platform portal compose](../../platform/21.portal/compose.yaml)
- [Authentik shared tasks](../../platform/10.authentik/shared_tasks.py)
