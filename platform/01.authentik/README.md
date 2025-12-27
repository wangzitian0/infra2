# Authentik (Platform SSO)

Identity Provider (IdP) for Single Sign-On and authentication across all platform services.

> **Module ID**: `platform/10.authentik`  
> **Category**: Auth & Gateway (10-19)  
> **Status**: 🏗️ In Design (Migrating to Init Container pattern)

## Dependencies

| Dependency | Source | Notes |
|------------|--------|-------|
| PostgreSQL | `platform/01.postgres` | Shared Platform PG |
| Redis | `platform/02.redis` | Caching and task queue |
| Vault Secrets | `secret/services/authentik/*` | Pulled via Init Container |

## Vault Secrets

The following secrets are stored in Vault:

| Path | Contents |
|------|----------|
| `secret/services/authentik/secret-key` | `AUTHENTIK_SECRET_KEY` |
| `secret/services/authentik/db-connection` | `PG_HOST`, `PG_USER`, `PG_PASS`, `PG_DB` |

## Architecture

```text
┌──────────────────────────────────────┐
│         Authentik Stack              │
├──────────────────────────────────────┤
│  vault-init (Init Container)        │
│    - Fetch secrets from Vault        │
│    - Write to shared volume          │
├──────────────────────────────────────┤
│  authentik-server                    │
│    - Web UI & API                    │
│    - Reads config from shared volume │
├──────────────────────────────────────┤
│  authentik-worker                    │
│    - Background jobs                 │
│    - Policy execution                │
└──────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────┐
│  PostgreSQL (01.postgres)            │
│  Redis (02.redis)                    │
└──────────────────────────────────────┘
```

## Compose Services

| Service | Image | Notes |
|---------|-------|-------|
| `vault-init` | `platform/_templates/vault-init` | Fetches secrets |
| `server` | `ghcr.io/goauthentik/server:2024.12` | Main app |
| `worker` | `ghcr.io/goauthentik/server:2024.12` | Background tasks |

## Domains

- **Production**: `auth.${INTERNAL_DOMAIN}`
- **Initial Setup**: `auth.${INTERNAL_DOMAIN}/if/flow/initial-setup/`

## Automation Tasks

| Task | Command | Description |
|------|---------|-------------|
| Setup Vault secrets | `invoke authentik.setup-vault` | Create Vault paths and write secrets |
| Prepare directories | `invoke authentik.prepare` | Create remote dirs on VPS |
| Deploy | `invoke authentik.deploy` | Deploy to Dokploy |
| Full setup | `invoke authentik.setup` | All of the above |

## Data Paths (VPS)

| Path | Contents |
|------|----------|
| `/data/platform/authentik/media` | Uploaded media files |
| `/data/platform/authentik/certs` | SSL certificates |

> **Note**: Database and cache data are stored in the shared `01.postgres` and `02.redis` modules.

## Migration from Monolithic Compose

This module is being refactored from a monolithic compose (with embedded Postgres/Redis) to:

1. **Shared Infrastructure**: Use shared `01.postgres` and `02.redis`
2. **Vault Integration**: Replace environment variable secrets with Init Container
3. **Modular Compose**: Only Authentik-specific services

## TODO

- [ ] Complete Vault path setup (`secret/services/authentik/`)
- [ ] Create AppRole and policy
- [ ] Rewrite `compose.yaml` with Init Container
- [ ] Remove embedded Postgres/Redis
- [ ] Test end-to-end flow

---

> **SSOT Reference**: [platform.secrets.md](../../docs/ssot/platform.secrets.md)  
> **Parent Module**: [platform/README.md](../README.md)
