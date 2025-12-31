# Platform Module

> **Purpose**: Stateful applications and shared infrastructure using Deployer pattern.

## Structure

| Range | Category | Services |
|-------|----------|----------|
| `01-09` | **Databases** | `01.postgres`, `02.redis` |
| `10-19` | **Auth & Gateway** | `10.authentik` |
| `20-29` | **Portal & Observability** | `21.portal` |

## Service Directory

```
platform/{nn}.{service}/
├── compose.yaml       # Docker Compose
├── deploy.py          # XxxDeployer + tasks
├── shared_tasks.py    # status() check
└── README.md          # Service docs
```

## Service Index

- [Postgres](./01.postgres/README.md)
- [Redis](./02.redis/README.md)
- [Authentik](./10.authentik/README.md)
- [Portal](./21.portal/README.md)

## Quick Start

```bash
# Deploy all (in order)
invoke postgres.setup
invoke redis.setup
invoke authentik.setup
invoke portal.setup

# Check status
invoke postgres.shared.status
invoke redis.shared.status
invoke authentik.shared.status
invoke portal.shared.status
```

Note: Deploy runs through Dokploy API; set `DOKPLOY_API_KEY` (or store it in 1Password) before running `*.setup`.

## Adding New Service

1. Create directory: `platform/{nn}.{service}/`

2. Create `deploy.py`:
   ```python
   import sys
   from libs.deployer import Deployer, make_tasks
   
   class NewDeployer(Deployer):
       service = "new"
       compose_path = "platform/XX.new/compose.yaml"
       data_path = "/data/platform/new"
       secret_key = "password"
       env_var_name = "NEW_PASSWORD"

   shared_tasks = sys.modules.get("platform.XX.new.shared")
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
       return check_service(c, "service", "health-cmd")
   ```

4. Run: `invoke new.setup`

## References

- **文档索引**: [docs/README.md](../docs/README.md)
- **Project Portfolio**: [docs/project/README.md](../docs/project/README.md)
- **AI 行为准则**: [AGENTS.md](../AGENTS.md)
- **SSOT**: [docs/ssot/platform.automation.md](../docs/ssot/platform.automation.md)
- **Libs**: [libs/README.md](../libs/README.md)
- **Tools**: [tools/README.md](../tools/README.md)
