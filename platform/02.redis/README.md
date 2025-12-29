# Platform Redis

> **Category**: Databases (01-09)

Shared Redis cache for all platform applications.

## Files

| File | Purpose |
|------|---------|
| `compose.yaml` | Docker Compose service definition |
| `deploy.py` | Invoke tasks (pre_compose/composing/post_compose/setup) |
| `shared_tasks.py` | Status checks |

## Deployment

```bash
# Full setup
invoke redis.setup

# Or step-by-step
invoke redis.pre-compose
invoke redis.composing
invoke redis.post-compose
```

## Data Path

`/data/platform/redis` - Redis persistence data

## Environment Variables

| Variable | Required |
|----------|----------|
| `REDIS_PASSWORD` | Yes |

> **Note**: `REDIS_PASSWORD` is sourced from Vault under the key `password`.

## Used By

- `10.authentik` (edge: `invoke redis.shared.status`)
