# Infra2 Shared Libraries

> **Purpose**: Internal libraries used by deploy scripts and tools.

## Modules

| Module | Purpose | Key Functions |
|--------|---------|---------------|
| `env.py` | **Core** Env & Secret SSOT | `EnvManager`, `get_or_set`, `op_get_item_field` |
| `common.py` | Utilities | `get_env()`, `validate_env()`, `load_env_keys()` |
| `console.py` | Rich CLI output | `success()`, `error()`, `header()`, `prompt_action()` |
| `deployer.py` | Deployment base class | `Deployer`, `load_shared_tasks()` |
| `config.py` | _Compatibility wrapper_ | `Config` class |

## Usage

### Quick Import
```python
from libs import Deployer, success, get_env
from libs.env import EnvManager
```

### Module Import
```python
from libs.env import EnvManager, get_or_set
from libs.common import load_env_keys
from libs.deployer import Deployer
```

## API Reference

### libs.env (New Core)

```python
class EnvManager:
    def __init__(project, env, service)
    def get_env(key, level) -> str
    def get_secret(key, level) -> str
    def set_secret(key, value) -> bool

def get_or_set(key, length=24) -> str
# Idempotent secret generation (check remote first)

def op_get_item_field(item_name, field_label, vault=OP_VAULT) -> str | None
# Read a specific 1Password item field (for non-standard items)
```

### libs.common

```python
get_env() -> dict
# Returns: {"VPS_HOST", "INTERNAL_DOMAIN", "PROJECT", "ENV"}

validate_env() -> list[str]
# Returns: list of missing required env vars

load_env_keys(path) -> list[str]
# Parse .env.example file for keys
```

### libs.deployer

```python
class Deployer:
    # ... attrs ...
    env_example_path: str = ".env.example"
    
    @classmethod
    def get_example_keys() -> list[str]
    # ... standard methods ...
```

### libs.config (Legacy)

Wrapper around `EnvManager` for backward compatibility.

Priority: service > environment > project

## References

- **SSOT**: [docs/ssot/platform.automation.md](../docs/ssot/platform.automation.md)
- **Platform**: [platform/README.md](../platform/README.md)
