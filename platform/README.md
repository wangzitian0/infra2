# Platform Module

> **Purpose**: Stateful applications and shared infrastructure using vault-init pattern.

## Architecture

Platform services use **vault-init pattern**:
- Secrets stored in Vault (`secret/platform/production/{service}`)
- Fetched at container runtime via vault-agent sidecar
- No secrets in Dokploy env vars or disk

## Structure

| Range | Category | Services |
|-------|----------|----------|
| `01-09` | **Databases** | `01.postgres`, `02.redis`, `03.clickhouse`, `03.minio` |
| `10-19` | **Auth & Gateway** | `10.authentik`, `11.signoz` |
| `20-29` | **Portal & Observability** | `21.portal` |

## Service Directory

```
platform/{nn}.{service}/
├── compose.yaml       # Docker Compose with vault-agent sidecar
├── deploy.py          # XxxDeployer + tasks
├── shared_tasks.py    # status() check
├── vault-agent.hcl    # Vault agent config
├── vault-policy.hcl   # Vault policy for service
├── secrets.ctmpl      # Template for secrets file
└── README.md          # Service docs
```

## Service Index

- [Postgres](./01.postgres/README.md)
- [Redis](./02.redis/README.md)
- [ClickHouse](./03.clickhouse/README.md)
- [MinIO](./03.minio/README.md)
- [Authentik](./10.authentik/README.md)
- [SigNoz](./11.signoz/README.md)
- [Portal](./21.portal/README.md)

## Prerequisites

1. **Vault ready**: `invoke vault.status` should return healthy
2. **Enable KV engine**: `vault secrets enable -path=secret kv-v2` (one-time)
3. **Setup tokens**: `export VAULT_ROOT_TOKEN=<token> && invoke vault.setup-tokens`

## Quick Start

```bash
# Deploy all (in dependency order)
# 1. Database tier
invoke postgres.setup    # Database for authentik
invoke redis.setup       # Cache for authentik
invoke clickhouse.setup  # Storage for signoz

# 2. Auth & Observability tier
invoke authentik.setup   # SSO provider
invoke signoz.setup      # Observability platform

# 3. Application tier
invoke portal.setup      # Portal with SSO auth

# Check status
invoke postgres.status
invoke redis.status
invoke clickhouse.status
invoke authentik.status
invoke signoz.status
invoke portal.status
```

## Deployment Order

Services must be deployed in order due to dependencies:

```
postgres ─┐
          ├──► authentik ──► portal
redis ────┘

clickhouse ──► signoz
```

| Service | Dependencies | Notes |
|---------|--------------|-------|
| postgres | vault | Database for authentik |
| redis | vault | Cache for authentik |
| clickhouse | - | Storage for signoz |
| authentik | postgres, redis | SSO provider |
| signoz | clickhouse | Observability platform |
| portal | authentik | Protected by SSO |

## Adding New Service

1. Create directory: `platform/{nn}.{service}/`

2. Create `deploy.py`:
   ```python
   import sys
   from libs.deployer import Deployer, make_tasks
   
   shared_tasks = sys.modules.get("platform.XX.new.shared")
   
   class NewDeployer(Deployer):
       service = "new"
       compose_path = "platform/XX.new/compose.yaml"
       data_path = "/data/platform/new"
       secret_key = "password"  # Key in Vault
       
       # Domain configuration:
       # - Set subdomain to auto-configure via Dokploy API (no SSO)
       # - Set subdomain = None to use compose.yaml Traefik labels (for SSO)
       subdomain = "new"  # or None for SSO-protected services
       service_port = 8080
       service_name = "server"  # For multi-service composes
   
   if shared_tasks:
       _tasks = make_tasks(NewDeployer, shared_tasks)
       status = _tasks["status"]
       pre_compose = _tasks["pre_compose"]
       composing = _tasks["composing"]
       post_compose = _tasks["post_compose"]
       setup = _tasks["setup"]
   ```

3. Create `shared_tasks.py`:
   ```python
   from invoke import task
   from libs.common import check_service
   
   @task
   def status(c):
       return check_service(c, "new", "health-cmd")
   ```

4. Copy vault-agent config from existing service and adapt

5. Run: `invoke new.setup`

## SSO Protection

Services can be protected by Authentik SSO using Traefik forward auth:

1. Set `subdomain = None` in deploy.py to disable Dokploy auto-domain
2. Add forwardauth middleware labels in compose.yaml:
   ```yaml
   labels:
     - "traefik.http.middlewares.{service}-auth.forwardauth.address=http://platform-authentik-server:9000/outpost.goauthentik.io/auth/traefik"
     - "traefik.http.middlewares.{service}-auth.forwardauth.trustForwardHeader=true"
     - "traefik.http.middlewares.{service}-auth.forwardauth.authResponseHeaders=X-authentik-username,X-authentik-groups,X-authentik-email,X-authentik-name,X-authentik-uid"
     - "traefik.http.routers.{service}.middlewares={service}-auth@docker"
   ```
3. Configure access control: `invoke authentik.shared.create-proxy-app --name={service} --slug={service} --external-host=https://{service}.{domain} --internal-host=platform-{service} --allowed-groups=admins`

See [docs/ssot/platform.sso.md](../docs/ssot/platform.sso.md) for details.

## References

- **文档索引**: [docs/README.md](../docs/README.md)
- **Project Portfolio**: [docs/project/README.md](../docs/project/README.md)
- **AI 行为准则**: [AGENTS.md](../AGENTS.md)
- **SSOT**: [docs/ssot/platform.automation.md](../docs/ssot/platform.automation.md)
- **Vault SSOT**: [docs/ssot/db.vault-integration.md](../docs/ssot/db.vault-integration.md)
- **Libs**: [libs/README.md](../libs/README.md)
- **Tools**: [tools/README.md](../tools/README.md)
