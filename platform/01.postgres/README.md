# Platform PostgreSQL

> **Category**: Databases (01-09)

Shared PostgreSQL database for platform services.

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
invoke postgres.setup

# Or step-by-step
invoke postgres.pre-compose
invoke postgres.composing
invoke postgres.post-compose
```

## Data Path

`/data/platform/postgres` - PostgreSQL data directory

## Environment Variables

| Variable | Required |
|----------|----------|
| `POSTGRES_PASSWORD` | Yes |
| `POSTGRES_USER` | No (default: postgres) |

## Used By

- `10.authentik`
