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
- `${DATA_PATH}/config.yml` - rendered config on VPS

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
- create `${DATA_PATH}`
- render `config.yml.tmpl` with `INTERNAL_DOMAIN` + `ENV_DOMAIN_SUFFIX`
- upload `${DATA_PATH}/config.yml`

## SSO Protection

Portal is protected by Authentik forward auth. Only users in `admins` group can access.

### Key Configuration

> **重要**：SSO 保护的服务必须禁用 Dokploy 自动域名配置，否则 compose.yaml 中的 forwardauth 中间件会被覆盖。

`deploy.py` 中设置 `subdomain = None`，让 compose.yaml 的 Traefik labels 生效。

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
  --external-host="https://home${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}" \
  --internal-host="platform-portal${ENV_SUFFIX}" \
  --port=8080
```

### Access Control

| User State | Result |
|-----------|--------|
| Not logged in | Redirect to `sso${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}` login |
| Logged in, not in `admins` | 403 Forbidden |
| Logged in, in `admins` | Access granted |

### Logout

Homer 页面右上角有 "Logout" 链接，指向 Authentik 的 end-session endpoint：
`https://sso${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}/application/o/portal/end-session/`

### Forward Auth Labels

The `compose.yaml` includes Traefik middleware labels for forward auth:

```yaml
labels:
  - "traefik.http.middlewares.portal-auth${ENV_DOMAIN_SUFFIX}.forwardauth.address=http://platform-authentik-server${ENV_SUFFIX}:9000/outpost.goauthentik.io/auth/traefik"
  - "traefik.http.middlewares.portal-auth${ENV_DOMAIN_SUFFIX}.forwardauth.trustForwardHeader=true"
  - "traefik.http.middlewares.portal-auth${ENV_DOMAIN_SUFFIX}.forwardauth.authResponseHeaders=X-authentik-username,X-authentik-groups,X-authentik-email,X-authentik-name,X-authentik-uid"
  - "traefik.http.routers.portal${ENV_DOMAIN_SUFFIX}.middlewares=portal-auth${ENV_DOMAIN_SUFFIX}@docker"
```

## Domain

`home${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}` - Portal homepage (SSO protected)

## Data Path

`${DATA_PATH}/config.yml` - Rendered Homer configuration

`config.yml` is mounted read-only to avoid accidental overwrites. If you need custom assets (logos/css), mount a writable `${DATA_PATH}/assets` to `/www/assets`.

## Environment Variables

`INTERNAL_DOMAIN` and `ENV_DOMAIN_SUFFIX` are injected into the Dokploy compose environment during `pre_compose` so the Traefik labels resolve. `INIT_ASSETS=0` is set to prevent Homer from overwriting the custom config.

## Updating Links

Edit `config.yml.tmpl` and re-run `invoke portal.pre_compose` to regenerate the config.

## Related

- [SSOT: Platform SSO](../../docs/ssot/platform.sso.md)
- [Authentik shared tasks](../10.authentik/shared_tasks.py)
