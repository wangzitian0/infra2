# Authentik (Platform SSO)

> **Category**: Auth & Gateway (10-19)

Identity Provider for Single Sign-On across platform services using vault-init pattern.

## Dependencies

- `01.postgres` must be deployed and healthy
- `02.redis` must be deployed and healthy
- Vault must have KV secrets engine enabled

## Files

| File | Purpose |
|------|---------|
| `compose.yaml` | Docker Compose (vault-agent + server + worker) |
| `deploy.py` | AuthentikDeployer with custom pre-compose |
| `shared_tasks.py` | Health check status() |
| `vault-agent.hcl` | Vault agent configuration |
| `vault-policy.hcl` | Read-only policy for all required secrets |
| `secrets.ctmpl` | Template rendering all secrets |

## Architecture

```
┌─────────────────┐
│ vault-agent     │ ──fetch──> Vault (3 paths)
│ (sidecar)       │            ├─ secret/platform/<env>/postgres (root_password)
└────────┬────────┘            ├─ secret/platform/<env>/redis (password)
         │ render              └─ secret/platform/<env>/authentik (secret_key)
         ▼
    /secrets/.env ─source─> server + worker containers
    (tmpfs)
```

## Deployment

```bash
# Ensure dependencies are ready
invoke postgres.status
invoke redis.status

# Full setup
invoke authentik.setup

# Or step-by-step
invoke authentik.pre-compose
invoke authentik.composing
invoke authentik.post-compose
```

**pre-compose** will:
1. Check postgres/redis secrets exist in Vault
2. Create data directories (`${DATA_PATH}/{media,certs}`)
3. Create `authentik` database in postgres
4. Generate and store `AUTHENTIK_SECRET_KEY` in Vault

## Vault Integration

**Secret paths**:
- `secret/platform/<env>/postgres` (reads `root_password`)
- `secret/platform/<env>/redis` (reads `password`)
- `secret/platform/<env>/authentik` (reads `secret_key`)

**Policy** (`platform-authentik-app`):
```hcl
path "secret/data/platform/{{env}}/postgres" {
  capabilities = ["read"]
}
path "secret/data/platform/{{env}}/redis" {
  capabilities = ["read"]
}
path "secret/data/platform/{{env}}/authentik" {
  capabilities = ["read", "list"]
}
```

## Domain

**URL**: `https://sso${ENV_SUFFIX}.${INTERNAL_DOMAIN}`

Configured via Traefik labels in `compose.yaml`.

## Data Path

`${DATA_PATH}` (uid=1000, gid=1000, chmod=755; staging uses `/data/platform/authentik-staging`)
- `media/` - User uploads and generated assets
- `certs/` - Custom certificates
- `custom-templates/` - Template overrides

## Containers

- **vault-agent**: Fetches secrets, renders to `/secrets/.env`
- **server**: Web UI and API (`platform-authentik-server${ENV_SUFFIX}`)
  - Port: 9000
  - Health: `ak healthcheck`
- **worker**: Background tasks (`platform-authentik-worker${ENV_SUFFIX}`)
  - No exposed ports
  - Health: `ak healthcheck`

## Initial Setup

1. Visit `https://sso${ENV_SUFFIX}.${INTERNAL_DOMAIN}/if/flow/initial-setup/`
2. Create admin user
3. Configure applications and flows

## Used By

- All platform services requiring authentication
