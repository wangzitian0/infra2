# Infra-005: Homer Portal Homepage

**Status**: In Progress  
**Owner**: Infra  
**Priority**: P2  

## Goal
Homer portal is reachable at https://home.zitian.party and provides links to core platform services.

## Context
Provide a unified homepage for platform services and a target for `PORTAL_URL` in E2E.

## Scope
- [ ] Add Homer portal service under `platform/21.portal`.
- [ ] Render config template and document the update flow.
- [ ] Add SSOT for `platform.portal` and update indexes/README.

## Deliverables
- Docker Compose + deploy tasks for portal
- Homer config template and generated config path
- SSOT + Platform README updates

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

## References
- [SSOT: platform.portal](../ssot/platform.portal.md)
- [Platform portal compose](../../platform/21.portal/compose.yaml)
- [Reference: 21.portal.tf](https://github.com/wangzitian0/infra/blob/main/platform/21.portal.tf)
