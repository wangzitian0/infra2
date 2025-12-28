# Infra2 Shared Libraries

> **Purpose**: Internal libraries used by deploy scripts and tools.

## Modules

| Module | Purpose | Key Functions |
|--------|---------|---------------|
| `common.py` | Environment & utilities | `get_env()`, `validate_env()`, `generate_password()`, `check_docker_service()` |
| `console.py` | Rich CLI output | `success()`, `error()`, `header()`, `env_vars()`, `run_with_status()` |
| `config.py` | Three-tier env loading | `Config` class |
| `deployer.py` | Deployment base class | `Deployer` class |

## Usage

### Quick Import
```python
from libs import Deployer, success, get_env
```

### Module Import
```python
from libs.common import check_docker_service
from libs.console import header, run_with_status
from libs.deployer import Deployer
from libs.config import Config
```

## API Reference

### libs.common

```python
get_env() -> dict
# Returns: {"VPS_HOST", "INTERNAL_DOMAIN", "PROJECT", "ENV"}

validate_env() -> list[str]
# Returns: list of missing required env vars

generate_password(length=24) -> str
# Returns: random alphanumeric password

check_docker_service(c, container, health_cmd, name) -> dict
# Returns: {"is_ready": bool, "details": str}
```

### libs.deployer

```python
class Deployer:
    service: str         # Service name
    compose_path: str    # Path to compose.yaml
    data_path: str       # Data directory on VPS
    uid, gid: str        # Owner UID/GID
    chmod: str           # Directory permissions
    secret_key: str      # Vault secret key name
    env_var_name: str    # Env var to display
    
    @classmethod
    def env() -> dict
    def vault_path() -> str
    def pre_compose(c) -> dict | None
    def store_secret(c, key, value) -> bool
    def read_secret(c, path, field) -> str | None
    def composing(c, env_keys)
    def post_compose(c, shared_tasks) -> bool
```

### libs.config

```python
class Config:
    def __init__(project, env='production', service=None)
    def get(key, level=None, default=None) -> str | None
    def all() -> dict
```

Priority: service > environment > project

## References

- **SSOT**: [docs/ssot/platform.automation.md](../docs/ssot/platform.automation.md)
- **Platform**: [platform/README.md](../platform/README.md)
