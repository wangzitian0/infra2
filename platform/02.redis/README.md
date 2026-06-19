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
│ vault-agent     │ ──fetch──> Vault (secret/platform/<env>/redis)
│ (sidecar)       │            └─ password
└────────┬────────┘
         │ render
         ▼
    /secrets/.env ─source─> redis container
    (tmpfs)
```

## Deployment

```bash
# Deploy through deploy_v2
python -m tools.deploy_v2 --service platform/redis --type staging --iac-ref vX.Y.Z --domain zitian.party
python -m tools.deploy_v2 --service platform/redis --type prod --iac-ref vX.Y.Z --domain zitian.party --code-reviewed

# Repair/debug only
invoke redis.pre-compose
invoke redis.composing
invoke redis.post-compose
```

## Vault Integration

**Secret path**: `secret/platform/<env>/redis`

**Required keys**:
- `password` - Redis password

**Policy** (`platform-redis-app`):
```hcl
path "secret/data/platform/{{env}}/redis" {
  capabilities = ["read", "list"]
}
```

## Data Path

`${DATA_PATH}` - Redis persistence data (uid=999, chmod=755, staging uses `/data/platform/redis-staging`)

## Container

- **Name**: `platform-redis${ENV_SUFFIX}`
- **Image**: `redis:7-alpine`
- **Port**: 6379 (internal only)
- **Health check**: `redis-cli ping`

## Used By

- `10.authentik` - Session and cache storage
