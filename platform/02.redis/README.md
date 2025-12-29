# Platform Redis

> **Category**: Databases (01-09)

Shared Redis cache for platform services.

## Files

| File | Purpose |
|------|---------|
| `compose.yaml` | Docker Compose service definition |
| `deploy.py` | Invoke tasks (pre_compose/composing/post_compose) |
| `shared_tasks.py` | Health check status() |
| `.env.example` | Environment template |

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

## Used By

- `10.authentik`
