# Infra2 Shared Libraries

> **Purpose**: Internal libraries used by deploy scripts and CLI tools.

## At a Glance

- `get_secrets` selects `OpSecrets` (1Password) or `VaultSecrets` (Vault) for SSOT reads/writes.
- `Deployer` + `make_tasks` standardize service deploy flows (now via Dokploy API).
- `dokploy` wraps the Dokploy REST API for compose deployments.
- `console` helpers keep CLI output consistent (Rich).
- `Config` is a legacy wrapper around secrets (avoid for new code).

## Module Map

| Module | Role | Key APIs |
|--------|------|----------|
| `env.py` | **Core** SSOT secrets access | `OpSecrets`, `VaultSecrets`, `get_secrets`, `generate_password` |
| `common.py` | Shared environment helpers | `get_env()`, `validate_env()`, `check_service()` |
| `console.py` | Rich CLI output | `header()`, `success()`, `error()`, `prompt_action()` |
| `deployer.py` | Deployment base class + task helpers | `Deployer`, `make_tasks()` |
| `dokploy.py` | Dokploy API client | `DokployClient`, `deploy_compose_service()` |
| `config.py` | Legacy compatibility wrapper | `Config` |

## Usage Patterns

### Secrets (SSOT-first)
```python
from libs.env import get_secrets

secrets = get_secrets(project="platform", service="postgres", env="production")
db_pass = secrets.get("POSTGRES_PASSWORD")
```

### Init seed vars (1Password)
```python
from libs.env import OpSecrets

init = OpSecrets()  # defaults to init/env_vars in Infra2 vault
vps_host = init.get("VPS_HOST")
```

### Deployer-based tasks
```python
from libs.deployer import Deployer, make_tasks
```

### Config (legacy wrapper)
```python
from libs.config import Config

config = Config(project="platform", env="production", service="postgres")
db_pass = config.get_secret("POSTGRES_PASSWORD")
```

## CLI Output Conventions

- Use `libs.console.header()` at task boundaries to anchor logs.
- Use `success()`/`warning()`/`error()`/`info()` for status lines; avoid raw `print`.
- Use `run_with_status()` for remote commands so success/error is consistent.
- Use `prompt_action()` for manual steps; keep instructions in the panel.
- Use `console.print()` only for raw values, Rich tables, or command blocks that must remain unwrapped.

## Notes

- Prefer explicit imports (e.g. `from libs.env import get_secrets`) over `from libs import ...` to avoid circular deps.
- `libs.common.get_env()` reads from `init/env_vars` in 1Password; no local `.env` required.
- `DokployClient.update_compose_env()` parses basic `KEY=VALUE` lines only (no quoted/escaped/multiline values).
- Dokploy API errors include method + endpoint context via `httpx` exceptions.
- `VaultSecrets` reads `VAULT_ROOT_TOKEN` and `VAULT_ADDR` (or falls back to `https://vault.$INTERNAL_DOMAIN`).

## References

- **Docs Index**: [docs/README.md](../docs/README.md)
- **Project Portfolio**: [docs/project/README.md](../docs/project/README.md)
- **AI 行为准则**: [AGENTS.md](../AGENTS.md)
- **SSOT**: [docs/ssot/platform.automation.md](../docs/ssot/platform.automation.md)
- **Core**: [docs/ssot/core.md](../docs/ssot/core.md)
- **Platform**: [platform/README.md](../platform/README.md)
