# Platform PostgreSQL

> **Category**: Databases (01-09)

Shared PostgreSQL database for all platform applications using vault-init pattern.

## Files

| File | Purpose |
|------|---------|
| `compose.yaml` | Docker Compose with vault-agent sidecar |
| `deploy.py` | PostgresDeployer with vault-init |
| `shared_tasks.py` | Health check + admin helpers |
| `vault-agent.hcl` | Vault agent configuration |
| `vault-policy.hcl` | Read-only policy for postgres secrets |
| `secrets.ctmpl` | Template rendering `POSTGRES_PASSWORD` |

## Architecture

```
┌─────────────────┐
│ vault-agent     │ ──fetch──> Vault (secret/platform/<env>/postgres)
│ (sidecar)       │            └─ root_password
└────────┬────────┘
         │ render
         ▼
    /secrets/.env ─source─> postgres container
    (tmpfs)
```

## Deployment

```bash
# Full setup (prepares dirs, ensures secrets, deploys)
invoke postgres.setup

# Or step-by-step
invoke postgres.pre-compose  # Creates dirs, checks Vault secret
invoke postgres.composing    # Deploys via Dokploy API
invoke postgres.post-compose # Verifies health
```

## Vault Integration

**Secret path**: `secret/platform/<env>/postgres`

**Required keys**:
- `root_password` - PostgreSQL root password

**Policy** (`platform-postgres-app`):
```hcl
path "secret/data/platform/{{env}}/postgres" {
  capabilities = ["read", "list"]
}
```

## Shared Tasks

```bash
invoke postgres.status  # Check if ready
invoke postgres.shared.create-database --name=<db>
invoke postgres.shared.create-user --username=<user> --database=<db> --password=<pass>
```

## Data Path

`${DATA_PATH}` - PostgreSQL data directory (uid=70, chmod=700, staging uses `/data/platform/postgres-staging`)

## Container

- **Name**: `platform-postgres${ENV_SUFFIX}`
- **Image**: `postgres:16-alpine`
- **Port**: 5432 (internal only)
- **Health check**: `pg_isready -U postgres`

## Used By

- `10.authentik` - Requires database `authentik`
