# Platform PostgreSQL

> **Category**: Databases (01-09)

Shared PostgreSQL database for all platform applications.

## Files

| File | Purpose |
|------|---------|
| `compose.yaml` | Docker Compose service definition |
| `deploy.py` | Invoke tasks (pre_compose/composing/post_compose/setup) |
| `shared_tasks.py` | Status + admin helpers |

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

> **Note**: `POSTGRES_PASSWORD` is sourced from Vault under the key `root_password`.

## Used By

- `10.authentik` (edge: `invoke postgres.shared.create-database`)
