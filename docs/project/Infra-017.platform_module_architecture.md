# Infra-017: Platform Module with Vault-Managed Secrets

**Status**: In Progress  
**Owner**: Infra  
**Created**: 2025-12-28

## Goal

Establish a layered Platform module architecture that separates infrastructure (databases) from applications, with all secrets managed via Vault and fetched by Init Containers at runtime.

## Context

Previously, Authentik was being added to the `bootstrap` module with embedded PostgreSQL and Redis, and secrets passed via environment variables. This doesn't scale as the platform grows. We need:

- **Separation of Concerns**: Databases (01-09) vs Apps (10+)
- **Secret Management**: Vault as the source of truth with AppRole-based access
- **Init Container Pattern**: Runtime secret fetching instead of compile-time injection
- **Modular Deployment**: Each service independently deployable

## Design Decisions

### 1. Module Numbering Convention

Decided to use a category-based numbering system:

| Range     | Category          | Rationale |
|-----------|-------------------|-----------|
| `01-09`   | **Databases**     | Shared infrastructure, low numbers for dependency ordering |
| `10-19`   | **Auth & Gateway**| Critical platform services |
| `20-29`   | **Observability** | Monitoring/logging tools |
| `30+`     | **Applications**  | Business applications |

**Rejected Alternative**: Alphabetical naming - harder to manage dependencies and category clarity.

### 2. Vault Directory Structure

Chose a hierarchical structure with strict policy isolation:

```
data/
├── _platform/      # Admin-only (Platform PG root, Vault master key)
├── _global/        # All apps read-only (company name, log level)
├── infrastructure/ # Database credentials (per DB type)
└── services/       # App-specific data (per app)
```

**Rationale**: Aligns with principle of least privilege. Apps only access their own `services/<app-name>` path plus necessary `infrastructure` paths.

### 3. Init Container vs. Direct Vault Client

Chose **Init Container pattern**:

```yaml
services:
  vault-init:
    image: vault:1.15
    command: sh -c "vault login ... && vault kv get ..."
    
  app:
    depends_on:
      vault-init:
        condition: service_completed_successfully
```

**Rationale**:
- ✅ Separation of concerns (data fetching vs app logic)
- ✅ Works with any app (no Vault SDK requirement)
- ✅ Data written to shared volume, isolated from app container
- ❌ Rejected embedding Vault client in app: Increases app complexity and SDK dependency

### 4. Documentation Structure

Per AGENTS.md guidelines:

- **Layer READMEs** (`platform/README.md`, `platform/10.authentik/README.md`): Directory intro, deployment steps
- **SSOT** (`docs/ssot/platform.data.md`): Detailed Vault structure, Init Container implementation, policies
- **Project** (this file): Design decisions, goals, implementation tracking
- **Onboarding** (`docs/onboarding/04.data.md`): How developers use Vault/Init Containers

## Scope

### Phase 1: Architecture & Documentation ✅
- [x] Create `platform/` directory structure
- [x] Move Authentik from `bootstrap/06.casdoor` to `platform/10.authentik`
- [x] Write `platform/README.md` (structure, numbering, deployment pattern)
- [x] Write `platform/10.authentik/README.md` (dependencies, migration plan)
- [x] Update `docs/ssot/platform.data.md` (Vault paths, AppRole, Init Container)
- [x] Update automation tasks for new paths

### Phase 2: Shared Infrastructure 🔄
- [ ] Create `platform/01.postgres/` (shared Platform PostgreSQL)
  - [ ] Compose with health checks
  - [ ] Tasks for DB user creation
  - [ ] Vault data storage for root password
- [ ] Create `platform/02.redis/` (shared cache)
  - [ ] Compose with persistence
  - [ ] Vault data storage for password

### Phase 3: Vault Integration 🔄
- [ ] Implement Vault AppRole automation
  - [ ] Create `platform/_lib/vault_manager.py`
  - [ ] Tasks for policy creation
  - [ ] Tasks for AppRole generation
- [ ] Create Init Container template
  - [ ] `platform/_templates/vault-init.Dockerfile`
  - [ ] `platform/_templates/vault-init.sh`

### Phase 4: Authentik Migration 📋
- [ ] Refactor Authentik to use shared infrastructure
  - [ ] Update compose to depend on `01.postgres` and `02.redis`
  - [ ] Add Init Container for data fetching
  - [ ] Remove embedded database containers
- [ ] Deploy and verify
  - [ ] Test AppRole login
  - [ ] Verify data fetching
  - [ ] Test end-to-end authentication flow

## Deliverables

- [ ] `platform/01.postgres/` module
- [ ] `platform/02.redis/` module
- [ ] `platform/10.authentik/` with Init Container
- [ ] `platform/_lib/vault_manager.py`
- [ ] `platform/_templates/` for reusable patterns
- [ ] Updated `docs/ssot/platform.data.md`
- [ ] Updated `docs/onboarding/04.data.md`

## Dependencies

- Bootstrap Vault must be initialized and unsealed
- 1Password Connect for initial data bootstrapping
- Dokploy for deployment

## PR Links

- In Progress: feat/authentik-deployment

## Change Log

- 2025-12-28: Project created, architecture documented
- 2025-12-28: Authentik moved to `platform/10.authentik`
- 2025-12-28: Documentation reorganized per AGENTS.md structure

## Verification

- [ ] All layer READMEs follow AGENTS.md guidelines
- [ ] SSOT contains detailed technical reference
- [ ] Onboarding guide enables developer self-service
- [ ] Init Container pattern tested end-to-end
- [ ] AppRole policies enforce least privilege
