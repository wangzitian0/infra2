# Authentik

Robust and feature-rich identity provider (IdP) for SSO and authentication.

## Status

⏭️ **Pending Deployment** - Configuration and automation tasks being created.

## Planning

### Components
- **Authentik Server**: Web interface, API, and core logic.
- **Authentik Worker**: Handles background tasks (e.g., policy execution, syncing).
- **PostgreSQL**: Primary database.
- **Redis**: Caching and task queue.

### Domains
- `auth.${INTERNAL_DOMAIN}`

### Integration
- **Vault**: Store long-term secrets.
- **1Password Connect**: Manage deployment-time credentials and initial secrets.

## Deployment Strategy

1. **Automation**: Use `invoke authentik.setup` to:
   - Create remote directories on VPS.
   - Generate secure secrets (SECRET_KEY, DB_PASS).
   - Prepare the `compose.yaml`.
2. **Persistence**: 
   - Postgres data: `/data/bootstrap/authentik/postgres`
   - Redis data: `/data/bootstrap/authentik/redis`
   - Media/Certs: `/data/bootstrap/authentik/media` / `/data/bootstrap/authentik/certs`
3. **Ingress**: Traefik integration via Docker labels.

## TODO

- [ ] Create `compose.yaml` (Base configuration).
- [ ] Create `tasks.py` (Automation logic).
- [ ] Configure Traefik routing labels.
- [ ] Test initial bootstrap and admin setup.
