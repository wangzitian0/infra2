# Authentik

> **Category**: Auth & Gateway  
> **Dependencies**: `01.postgres`, `02.redis` (TODO), Vault  
> **Status**: 🏗️ Migrating to Init Container pattern

Identity Provider (IdP) for Single Sign-On and authentication across all platform services.

## Quick Links

- **Domain**: `auth.${INTERNAL_DOMAIN}`
- **Initial Setup**: `auth.${INTERNAL_DOMAIN}/if/flow/initial-setup/`
- **Vault Secrets**: `secret/services/authentik/*`

## Components

| Service | Image | Purpose |
|---------|-------|---------|
| `server` | `ghcr.io/goauthentik/server:2024.12` | Web UI & API |
| `worker` | `ghcr.io/goauthentik/server:2024.12` | Background tasks |

## Dependencies

- **PostgreSQL**: Shared from `platform/01.postgres` (TODO: migrate from embedded)
- **Redis**: Shared from `platform/02.redis` (TODO: migrate from embedded)
- **Vault**: Secrets at `secret/services/authentik/`

## Deployment

```bash
# 1. Setup Vault secrets and policies
invoke authentik.setup-vault

# 2. Prepare VPS directories
invoke authentik.prepare

# 3. Deploy to Dokploy
invoke authentik.deploy
```

## Current State (Temporary)

Currently using embedded PostgreSQL and Redis. Migration plan:

- [ ] Extract database to `01.postgres`
- [ ] Extract cache to `02.redis`
- [ ] Add Init Container for Vault secrets
- [ ] Update compose to use shared infrastructure

## Data Paths

| Path | Contents |
|------|----------|
| `/data/platform/authentik/postgres` | Database (temp, will migrate) |
| `/data/platform/authentik/redis` | Cache (temp, will migrate) |
| `/data/platform/authentik/media` | Uploaded files |
| `/data/platform/authentik/certs` | SSL certificates |

## References

- **Vault Secret Structure**: [docs/ssot/platform.secrets.md](../../docs/ssot/platform.secrets.md)
- **Init Container Pattern**: [docs/ssot/platform.secrets.md#init-container-pattern](../../docs/ssot/platform.secrets.md)
