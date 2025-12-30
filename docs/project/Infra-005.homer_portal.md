# Infra-005: Homer Portal + DNS/TLS Automation

**Status**: In Progress  
**Owner**: Infra  
**Priority**: P2  

## Goal
`home.zitian.party` 可访问，同时 Cloudflare DNS 与证书设置自动化覆盖 `cloud/op/vault/sso/home`。

## Context
统一入口页 + 自动化 DNS/证书，保证 0 帧起手可复现。

## Scope
- [x] Add Homer portal service under `platform/21.portal`.
- [x] Render config template and document the update flow.
- [x] Automate Cloudflare DNS records for `cloud/op/vault/sso/home`.
- [x] Automate Cloudflare SSL settings and HTTPS warm-up.
- [x] Add SSOT for DNS/cert automation and update Bootstrap docs.

## Deliverables
- Docker Compose + deploy tasks for portal
- Homer config template and generated config path
- Platform README updates
- Cloudflare DNS automation tasks + .env.example
- DNS/TLS SSOT + Bootstrap README updates

## PR Links
- None yet.

## Change Log
| Date | Change |
|------|--------|
| 2025-12-30 | Initialized project |

## Verification
- [ ] `invoke portal.shared.status`
- [ ] `https://home.${INTERNAL_DOMAIN}` loads portal
- [ ] `PORTAL_URL` set for E2E (optional)
- [ ] `invoke dns_and_cert.verify`

## Open Issues
- [ ] Deploy the portal app in Dokploy and confirm `https://home.${INTERNAL_DOMAIN}`.
- [ ] Confirm `bootstrap/cloudflare` item fields are complete (`CF_API_TOKEN`, `CF_ZONE_ID`/`CF_ZONE_NAME`, optional `CF_RECORDS`).
- [ ] Dokploy Server Domain SSL is still manual; evaluate API/CLI automation.

## References
- [SSOT: bootstrap.dns_and_cert](../ssot/bootstrap.dns_and_cert.md)
- [Portal README](../../platform/21.portal/README.md)
- [Platform portal compose](../../platform/21.portal/compose.yaml)
- [Reference: 21.portal.tf](https://github.com/wangzitian0/infra/blob/main/platform/21.portal.tf)
