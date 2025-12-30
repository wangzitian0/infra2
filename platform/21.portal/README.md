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

## Source of Truth

- `platform/21.portal/compose.yaml` - service definition
- `platform/21.portal/config.yml.tmpl` - Homer links and layout
- `/data/platform/portal/config.yml` - rendered config on VPS

## Deployment

```bash
# Full setup
invoke portal.setup

# Or step-by-step
invoke portal.pre_compose
invoke portal.composing
invoke portal.post_compose
```

`pre_compose` will:
- create `/data/platform/portal`
- render `config.yml.tmpl` with `INTERNAL_DOMAIN`
- upload `/data/platform/portal/config.yml`

## Domain

`home.${INTERNAL_DOMAIN}` - Portal homepage

## Data Path

`/data/platform/portal/config.yml` - Rendered Homer configuration

`config.yml` is mounted read-only to avoid accidental overwrites. If you need custom assets (logos/css), mount a writable `/data/platform/portal/assets` to `/www/assets`.

## Environment Variables

None required (uses `INTERNAL_DOMAIN` from init/env_vars). `INIT_ASSETS=0` is set to prevent Homer from overwriting the custom config.

## Updating Links

Edit `config.yml.tmpl` and re-run `invoke portal.pre_compose` to regenerate the config.
