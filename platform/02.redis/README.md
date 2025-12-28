# Platform Redis

> **Category**: Databases (01-09)

Shared Redis cache for all platform applications.

## Files

| File | Purpose |
|------|---------|
| `compose.yaml` | Docker Compose service definition |
| `pre-compose.py` | Prepare directories, generate password |
| `post-compose.py` | Verify health, ensure ready |

## Deployment

```bash
# 1. Pre-compose (prepare + generate password)
python platform/02.redis/pre-compose.py

# 2. Deploy in Dokploy
#    - Compose Path: platform/02.redis/compose.yaml
#    - Add REDIS_PASSWORD env var

# 3. Post-compose (verify)
python platform/02.redis/post-compose.py
```

## Data Path

`/data/platform/redis` - Redis persistence data

## Environment Variables

| Variable | Required |
|----------|----------|
| `REDIS_PASSWORD` | Yes |

## Used By

- `10.authentik` (edge: `02.verify_redis.py`)
