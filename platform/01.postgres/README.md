# Platform PostgreSQL

> **Category**: Databases (01-09)

Shared PostgreSQL database for all platform applications.

## Files

| File | Purpose |
|------|---------|
| `compose.yaml` | Docker Compose service definition |
| `pre-compose.py` | Prepare directories, generate password |
| `post-compose.py` | Verify health, ensure ready |

## Deployment

```bash
# 1. Pre-compose (prepare + generate password)
python platform/01.postgres/pre-compose.py

# 2. Deploy in Dokploy
#    - Compose Path: platform/01.postgres/compose.yaml
#    - Add POSTGRES_PASSWORD env var

# 3. Post-compose (verify)
python platform/01.postgres/post-compose.py
```

## Data Path

`/data/platform/postgres` - PostgreSQL data directory

## Environment Variables

| Variable | Required |
|----------|----------|
| `POSTGRES_PASSWORD` | Yes |
| `POSTGRES_USER` | No (default: postgres) |

## Used By

- `10.authentik` (edge: `01.create_database.py`)
