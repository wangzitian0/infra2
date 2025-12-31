# Platform Redis

> **Category**: Databases (01-09)

Shared Redis cache for all platform applications using vault-init pattern.

## Files

| File | Purpose |
|------|---------|
| `compose.yaml` | Docker Compose with vault-agent sidecar |
| `deploy.py` | RedisDeployer with vault-init |
| `shared_tasks.py` | Health check status() |
| `vault-agent.hcl` | Vault agent configuration |
| `vault-policy.hcl` | Read-only policy for redis secrets |
| `secrets.ctmpl` | Template rendering `REDIS_PASSWORD` |

## Architecture

```
┌─────────────────┐
│ vault-agent     │ ──fetch──> Vault (secret/platform/production/redis)
│ (sidecar)       │            └─ password
└────────┬────────┘
         │ render
         ▼
    /secrets/.env ─source─> redis container
    (tmpfs)
```

## Deployment

```bash
# Full setup
invoke redis.setup

# Or step-by-step
invoke redis.pre-compose
invoke redis.composing
invoke redis.post-compose
```

## Vault Integration

**Secret path**: `secret/platform/production/redis`

**Required keys**:
- `password` - Redis password

**Policy** (`platform-redis-app`):
```hcl
path "secret/data/platform/production/redis" {
  capabilities = ["read", "list"]
}
```

## Data Path

`/data/platform/redis` - Redis persistence data (uid=999, chmod=755)

## Container

- **Name**: `platform-redis`
- **Image**: `redis:7-alpine`
- **Port**: 6379 (internal only)
- **Health check**: `redis-cli ping`

## Used By

- `10.authentik` - Session and cache storage
