# Platform Module

> **Purpose**: Stateful applications and shared infrastructure using Deployer pattern.

## Structure

| Range | Category | Services |
|-------|----------|----------|
| `01-09` | **Databases** | `01.postgres`, `02.redis` |
| `10-19` | **Auth & Gateway** | `10.authentik` |
| `20-29` | **Observability** | (future) |

## Service Directory

```
platform/{nn}.{service}/
├── compose.yaml       # Docker Compose
├── deploy.py          # XxxDeployer + tasks
├── shared_tasks.py    # status() check
├── .env.example       # Env template
└── README.md          # Service docs
```

## Quick Start

```bash
# Deploy all (in order)
invoke postgres.setup
invoke redis.setup
invoke authentik.setup

# Check status
invoke postgres.shared.status
invoke redis.shared.status
invoke authentik.shared.status
```

## Adding New Service

1. Create directory: `platform/{nn}.{service}/`

2. Create `deploy.py`:
   ```python
   from libs.deployer import Deployer
   from libs.console import success
   from invoke import task
   
   class NewDeployer(Deployer):
       service = "new"
       compose_path = "platform/XX.new/compose.yaml"
       data_path = "/data/platform/new"
       secret_key = "password"
       env_var_name = "NEW_PASSWORD"
   
   @task
   def pre_compose(c):
       return NewDeployer.pre_compose(c)
   
   @task
   def composing(c):
       NewDeployer.composing(c)
   
   @task
   def post_compose(c):
       from . import shared_tasks
       return NewDeployer.post_compose(c, shared_tasks)
   
   @task(pre=[pre_compose, composing, post_compose])
   def setup(c):
       success(f"{NewDeployer.service} setup complete!")
   ```

3. Create `shared_tasks.py`:
   ```python
   from invoke import task
   from libs.common import check_docker_service
   
   @task
   def status(c):
       return check_docker_service(c, "container-name", "health-cmd", "Service")
   ```

4. Run: `invoke new.setup`

## References

- **SSOT**: [docs/ssot/platform.automation.md](../docs/ssot/platform.automation.md)
- **Libs**: [libs/README.md](../libs/README.md)
- **Tools**: [tools/README.md](../tools/README.md)
