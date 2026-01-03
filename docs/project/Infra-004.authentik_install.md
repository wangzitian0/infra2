# Infra-004: Authentik Installation

**Status**: Completed ✅
**Owner**: Infra  
**Priority**: P2  
**Branch**: `refactor/platform-dry` (PR #28)

## Goal

Install Authentik SSO for identity management on platform services with improved error handling and deployment reliability.

## Scope

- [x] Create deployment automation framework (libs/deployer.py)
- [x] Deploy PostgreSQL (01.postgres) with vault-init
- [x] Deploy Redis (02.redis) with vault-init
- [x] Deploy Authentik (10.authentik) with vault-init
- [x] Automate Vault Token generation and Dokploy configuration
- [x] **DRY refactor**: Reduce platform deploy.py from 282 to 115 lines (-167 lines)
- [x] **Domain auto-config**: Subdomain configuration via Dokploy API
- [x] **Bootstrap admin**: Auto-create admin with credentials in Vault
- [x] **Error handling**: Fatal/check_failed/error classification

## Architecture

```mermaid
flowchart LR
    Vault[HashiCorp Vault] -->|vault-agent| Secrets[/secrets/.env tmpfs]
    Secrets --> App[Application Container]
    PG[01.postgres] --> AUTH[10.authentik]
    RD[02.redis] --> AUTH
    AUTH --> SSO[https://sso.zitian.party]
```

## Key Achievements

### 1. DRY Refactor (-167 lines)
| File | Before | After | Reduced |
|------|--------|-------|---------|
| postgres/deploy.py | 68 | 23 | -45 |
| redis/deploy.py | 61 | 21 | -40 |
| authentik/deploy.py | 153 | 71 | -82 |

**Improvements**:
- Moved vault-init logic to `Deployer.pre_compose()`
- Unified secret generation pattern
- Used `make_tasks()` for all services

### 2. Domain Auto-Configuration
```python
class AuthentikDeployer(Deployer):
    subdomain = "sso"
    service_port = 9000
    service_name = "server"
```
Automatically configures `https://sso.{INTERNAL_DOMAIN}` via Dokploy API after deployment.

### 3. Bootstrap Admin Credentials
- **Email**: Set from `ADMIN_EMAIL` env or default
- **Password**: Random 24-char, stored in Vault `bootstrap_password`
- **Created**: On first deployment via `AUTHENTIK_BOOTSTRAP_*` env vars

### 4. Improved Error Handling
- **Fatal errors**: Pre-flight checks with actionable guidance
  ```
  FATAL: VAULT_ROOT_TOKEN not set
    Get token: op read 'op://Infra2/dexluuvzg5paff3cltmtnlnosm/Root Token' (item: bootstrap/vault/Root Token)
    If field name is Token, use: op://Infra2/dexluuvzg5paff3cltmtnlnosm/Token
    Then: export VAULT_ROOT_TOKEN=<token>
  ```
- **Idempotent operations**: `CREATE DATABASE IF NOT EXISTS` pattern
- **Clear failure causes**: Error messages explain impact and resolution

### 5. Password Management SSOT
- **Web UI passwords** → 1Password (browser autofill)
- **Machine passwords** → Vault (vault-agent autofetch)
- **Sync workflow**: Vault → 1Password for admin credentials

## Deployment Commands

```bash
# 1. Generate Tokens (One-time)
export VAULT_ROOT_TOKEN=$(op read 'op://Infra2/dexluuvzg5paff3cltmtnlnosm/Root Token') # item: bootstrap/vault/Root Token
# If field name is Token, use: op://Infra2/dexluuvzg5paff3cltmtnlnosm/Token
invoke vault.setup-tokens

# 2. Deploy Services
invoke postgres.setup
invoke redis.setup
invoke authentik.setup

# 3. Get Admin Credentials
vault kv get -field=bootstrap_password secret/platform/<env>/authentik
vault kv get -field=bootstrap_email secret/platform/<env>/authentik
```

## Verification

- [x] `invoke --list` loads all modules
- [x] `invoke postgres.status` returns healthy
- [x] `invoke redis.status` returns healthy
- [x] `invoke authentik.status` returns healthy
- [x] Authentik UI at https://sso.zitian.party reachable
- [x] Bootstrap admin can login

## Credentials

**Authentik Admin**:
- URL: `https://sso.zitian.party`
- Username: `akadmin`
- Email: Stored in Vault & 1Password
- Password: Stored in Vault & 1Password
- Vault path: `secret/platform/<env>/authentik` (keys: `bootstrap_email`, `bootstrap_password`)
- 1Password: `platform/authentik/admin`

## TODOWRITE

- [Infra-004.TODOWRITE.md](./Infra-004.TODOWRITE.md)

## References

- [SSOT: platform.automation.md](../ssot/platform.automation.md)
- [SSOT: bootstrap.vars_and_secrets.md](../ssot/bootstrap.vars_and_secrets.md)
- [PR #28](https://github.com/wangzitian0/infra2/pull/28)
