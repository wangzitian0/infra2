# Infra-004: TODOWRITE (Authentik Installation)

**Status**: Active  
**Owner**: Infra

## Purpose
Track issues and improvements discovered during Authentik installation and platform deployment.

## Top Issues / Improvements

### Deployment Experience
- [ ] Add `invoke authentik.reset` command to reset database cleanly
- [ ] Add `--dry-run` mode for pre-flight dependency checks
- [ ] Integrate last 10 lines of logs into `status` command when unhealthy
- [ ] Visualize dependency tree with status (like `tree` command)

### Error Handling (In Progress)
- [x] Added `fatal()` for unrecoverable errors with actionable guidance
- [x] Added `check_failed()` for non-fatal warnings
- [x] Pre-flight check for `VAULT_ROOT_TOKEN` before operations
- [ ] Pre-flight check for postgres/redis health before authentik deploy
- [ ] Retry logic for transient failures (network, container startup)

### Documentation
- [x] Document password classification (Web UI vs Machine)
- [x] Add Vault → 1Password sync workflow
- [ ] Create troubleshooting guide for common deployment failures
- [ ] Add deployment state machine diagram
- [ ] Document recovery procedures for each service

### Code Quality
- [x] DRY refactor: -167 lines of duplicate code
- [x] Domain auto-configuration via Dokploy API
- [ ] Type hints for all deployer methods
- [ ] Unit tests for deployer base class
- [ ] Integration test for full platform deployment

### Secrets Management
- [x] Bootstrap admin credentials in Vault
- [ ] Auto-sync Web UI passwords to 1Password after generation
- [ ] Rotate bootstrap credentials periodically
- [ ] Audit trail for secret access

### Monitoring
- [ ] Health check dashboard (all services at a glance)
- [ ] Alert on service unhealthy for >5 minutes
- [ ] Track deployment success/failure metrics
- [ ] Log aggregation for all platform services
