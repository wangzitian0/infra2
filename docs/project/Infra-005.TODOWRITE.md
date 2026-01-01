# Infra-005: TODOWRITE (Homer Portal + SSO)

**Status**: Active  
**Owner**: Infra

## Purpose
Track top issues discovered during the Homer portal and SSO protection project.

## Top Issues (Top 30)

### SSO Integration
- [ ] **Create Authentik Root Token** - Run `invoke authentik.shared.create-root-token`
- [ ] **Setup admin group** - Run `invoke authentik.shared.setup-admin-group`
- [ ] **Create Portal SSO app** - Run `invoke authentik.shared.create-proxy-app --name=Portal --slug=portal --external-host=https://home.zitian.party --internal-host=platform-portal${ENV_SUFFIX}`
- [ ] **Test access flow** - Verify unauthenticated → login → access works
- [ ] **Test denial flow** - Verify non-admin users get access denied

### Code Quality
- [ ] Authentik API client could be extracted to `libs/authentik.py` for reuse
- [ ] Policy binding logic may need refinement (currently creates per-group policies)
- [ ] Error handling for existing applications (handle 409 conflict)

### Documentation
- [x] Update Infra-005 project doc with SSO scope
- [x] Create SSOT `platform.sso.md` for SSO design and usage
- [x] Update Portal README with SSO configuration info
- [x] Update Authentik README with shared tasks documentation

### Infrastructure
- [ ] token-init compose service needs testing on fresh deploy
- [ ] Consider adding `is_superuser=true` for admins group (Authentik superuser)
- [ ] Evaluate if we need per-app tokens (AUTHENTIK_APP_TOKEN) for future services

### Future Improvements
- [ ] Add `delete-proxy-app` task for cleanup
- [ ] Add `add-user-to-group` task for user management
- [ ] Add Authentik backup/restore automation
- [ ] Consider LDAP/SCIM integration for user sync
