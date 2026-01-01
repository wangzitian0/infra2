# Homer Portal

> **Category**: Portal (20-29)

Static homepage for platform services, powered by Homer. Protected by Authentik SSO.

## Files

| File | Purpose |
|------|---------|
| `compose.yaml` | Docker Compose + SSO forward auth labels |
| `deploy.py` | Invoke tasks (pre_compose/composing/post_compose/setup) |
| `shared_tasks.py` | Status checks |
| `config.yml.tmpl` | Homer config template (rendered before deploy) |

## Source of Truth

- `platform/21.portal/compose.yaml` - service definition + SSO labels
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

## SSO Protection

Portal is protected by Authentik forward auth. Only users in `admins` group can access.

### Setup SSO (one-time after Authentik deploy)

```bash
# 1. Create Authentik Root Token
export VAULT_ROOT_TOKEN=<vault-admin-token>
invoke authentik.shared.create-root-token

# 2. Setup admin group
invoke authentik.shared.setup-admin-group

# 3. Create Portal SSO application
invoke authentik.shared.create-proxy-app \
  --name="Portal" \
  --slug="portal" \
  --external-host="https://home.${INTERNAL_DOMAIN}" \
  --internal-host="platform-portal" \
  --port=8080
```

### Access Control

| User State | Result |
|-----------|--------|
| Not logged in | Redirect to `sso.${INTERNAL_DOMAIN}` login |
| Logged in, not in `admins` | 403 Forbidden |
| Logged in, in `admins` | Access granted |

### Forward Auth Labels

The `compose.yaml` includes Traefik middleware labels for forward auth:

```yaml
labels:
  - "traefik.http.middlewares.portal-auth.forwardauth.address=http://platform-authentik-server:9000/outpost.goauthentik.io/auth/traefik"
  - "traefik.http.middlewares.portal-auth.forwardauth.trustForwardHeader=true"
  - "traefik.http.middlewares.portal-auth.forwardauth.authResponseHeaders=X-authentik-username,X-authentik-groups,X-authentik-email,X-authentik-name,X-authentik-uid"
  - "traefik.http.routers.portal.middlewares=portal-auth@docker"
```

## Domain

`home.${INTERNAL_DOMAIN}` - Portal homepage (SSO protected)

## Data Path

`/data/platform/portal/config.yml` - Rendered Homer configuration

`config.yml` is mounted read-only to avoid accidental overwrites. If you need custom assets (logos/css), mount a writable `/data/platform/portal/assets` to `/www/assets`.

## Environment Variables

`INTERNAL_DOMAIN` is injected into the Dokploy compose environment during `pre_compose` so the Traefik labels resolve. `INIT_ASSETS=0` is set to prevent Homer from overwriting the custom config.

## Updating Links

Edit `config.yml.tmpl` and re-run `invoke portal.pre_compose` to regenerate the config.

## Related

- [SSOT: Platform SSO](../../docs/ssot/platform.sso.md)
- [Authentik shared tasks](../10.authentik/shared_tasks.py)
