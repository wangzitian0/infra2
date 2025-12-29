# Homer Portal

> **Category**: Portal (20-29)

Static homepage for platform services, powered by Homer.

## Files

| File | Purpose |
|------|---------|
| `compose.yaml` | Docker Compose service definition |
| `deploy.py` | Invoke tasks (pre_compose/composing/post_compose/setup) |
| `shared_tasks.py` | Status checks |
| `config.yml.tmpl` | Homer config template (rendered before deploy) |

## Deployment

```bash
# Full setup
invoke portal.setup

# Or step-by-step
invoke portal.pre-compose
invoke portal.composing
invoke portal.post-compose
```

`pre-compose` will:
- create `/data/platform/portal`
- render `config.yml.tmpl` with `INTERNAL_DOMAIN`
- upload `/data/platform/portal/config.yml`

## Domain

`home.${INTERNAL_DOMAIN}` - Portal homepage

## Data Path

`/data/platform/portal/config.yml` - Rendered Homer configuration

## Environment Variables

None required (uses `INTERNAL_DOMAIN` from init/env_vars).

## Updating Links

Edit `config.yml.tmpl` and re-run `invoke portal.pre-compose` to regenerate the config.
