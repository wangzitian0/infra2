# Authentik (Platform SSO)

> **Category**: Auth & Gateway (10-19)

Identity Provider for Single Sign-On across platform services.

## Dependencies

- `01.postgres` must be deployed
- `02.redis` must be deployed

## Files

| File | Purpose |
|------|---------|
| `compose.yaml` | Docker Compose (server + worker) |
| `deploy.py` | Invoke tasks with custom pre_compose logic |
| `shared_tasks.py` | Health check status() |
| `.env.example` | Environment template |

## Deployment

```bash
# Ensure dependencies are ready
invoke postgres.shared.status
invoke redis.shared.status

# Full setup
invoke authentik.setup

# Or step-by-step
invoke authentik.pre-compose
invoke authentik.composing
invoke authentik.post-compose
```

`pre-compose` will:
- create data directories
- read DB/Redis passwords from Vault (fallback to manual)
- create the authentik database
- generate `AUTHENTIK_SECRET_KEY`

## Domain

`sso.${INTERNAL_DOMAIN}` - Authentik Web UI

## Environment Variables

| Variable | Required |
|----------|----------|
| `AUTHENTIK_SECRET_KEY` | Yes |
| `PG_PASS` | Yes (same as 01.postgres) |
| `REDIS_PASSWORD` | Yes (same as 02.redis) |
| `PG_USER` | No (default: authentik) |
| `PG_DB` | No (default: authentik) |
