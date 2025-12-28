# Authentik (Platform SSO)

> **Category**: Auth & Gateway (10-19)

Identity Provider for Single Sign-On across all platform services.

## Dependencies (Edges)

Must execute edges before deploying:

| Edge | Source | Purpose |
|------|--------|---------|
| `01.create_database.py` | 01.postgres | Create authentik database |
| `02.verify_redis.py` | 02.redis | Verify Redis accessible |

## Files

| File | Purpose |
|------|---------|
| `compose.yaml` | Docker Compose (server + worker) |
| `pre-compose.py` | Prepare directories, generate secret key |
| `post-compose.py` | Verify health, display setup URL |
| `01.create_database.py` | Edge: Create DB in postgres |
| `02.verify_redis.py` | Edge: Verify Redis |

## Deployment

```bash
# 1. Ensure dependencies are deployed
python platform/01.postgres/post-compose.py
python platform/02.redis/post-compose.py

# 2. Run edges
python platform/10.authentik/01.create_database.py
python platform/10.authentik/02.verify_redis.py

# 3. Pre-compose
python platform/10.authentik/pre-compose.py

# 4. Deploy in Dokploy
#    - Compose Path: platform/10.authentik/compose.yaml
#    - Add env vars: AUTHENTIK_SECRET_KEY, PG_PASS, REDIS_PASSWORD

# 5. Post-compose
python platform/10.authentik/post-compose.py
```

## Domain

`sso.${INTERNAL_DOMAIN}` - SSO Web UI

## Environment Variables

| Variable | Required |
|----------|----------|
| `AUTHENTIK_SECRET_KEY` | Yes |
| `PG_PASS` | Yes (same as 01.postgres) |
| `REDIS_PASSWORD` | Yes (same as 02.redis) |
| `PG_USER` | No (default: authentik) |
| `PG_DB` | No (default: authentik) |
