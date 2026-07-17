# Infra2 Internal Libraries

> **Purpose**: Internal libraries used by deploy scripts and CLI tools. Stable
> cross-repository contracts live in
> [`infra2-sdk`](https://github.com/wangzitian0/infra2-sdk) and are imported directly.
> The infra2 release pin is `v0.3.0`; adoption and Canary installation are
> equality-guarded by `libs/tests/test_sdk_contract_adoption.py`.

## At a Glance

- `get_secrets` selects `OpSecrets` (1Password) or `VaultSecrets` (Vault) for SSOT reads/writes.
- `Deployer` + `make_tasks` standardize service deploy flows (now via Dokploy API).
- `iac_runner_client` signs exact operation requests and polls by deployment ID.
- `dokploy` wraps the Dokploy REST API for compose deployments.
- `backup_restore` verifies off-host backup manifests and builds guarded restore rehearsal plans.
- `console` helpers keep CLI output consistent (Rich).

## Module Map

| Module | Role | Key APIs |
|--------|------|----------|
| `env.py` | **Core** SSOT secrets access | `OpSecrets`, `VaultSecrets`, `get_secrets`, `generate_password` |
| `common.py` | Shared environment helpers | `get_env()`, `validate_env()`, `check_service()` |
| `console.py` | Rich CLI output | `header()`, `success()`, `error()`, `prompt_action()` |
| `deployer.py` | Deployment base class + task helpers | `Deployer`, `make_tasks()` |
| `iac_runner_client.py` | Signed IaC Runner operation client | `trigger_platform_deploy()`, `poll_platform_deploy_status()` |
| `dokploy.py` | Dokploy API client | `DokployClient`, `get_dokploy()` |
| `backup_restore.py` | Off-host backup restore rehearsal helpers | `latest_artifact_for_service()`, `build_postgres_rehearsal_plan()`, `run_postgres_restore_rehearsal()` |
| `dokploy_route_canary.py` | Dynamic route canary | `run_route_canary()`, `render_canary_compose()` |
| `app_deploy_request.py` | Fail-closed App request validation, Production evidence verification, and deploy planning | `verify_production_evidence()`, `validate_request_authority()`, `make_plan()` |
| `harness_manifest.py` | Read-only workspace inventory and autonomy-boundary validation | `load_manifest()`, `validate_manifest()`, `check_workspace()` |

## Usage Patterns

### Secrets (SSOT-first)

`get_secrets()` routes to the appropriate backend based on `type` parameter:

| Type | Backend | Path Format |
|------|---------|-------------|
| `None` (default) | Vault | `secret/data/{project}/{env}/{service}` |
| `'app_vars'` | Vault | `secret/data/{project}/{env}/{service}` |
| `'bootstrap'` | 1Password | `{project}/{service}` (no env) |
| `'root_vars'` | 1Password | `{project}/{env}/{service}` |

```python
from libs.env import get_secrets

# App vars (Vault, default)
secrets = get_secrets(project="platform", service="postgres", env="production")
db_pass = secrets.get("POSTGRES_PASSWORD")

# Bootstrap credentials (1Password, no env layer)
bootstrap = get_secrets(project="bootstrap", service="vault", type="bootstrap")
root_token = bootstrap.get("ROOT_TOKEN")

# Root vars (1Password, with env layer, for Web UI passwords)
root_vars = get_secrets(project="platform", env="production", service="authentik", type="root_vars")
admin_pass = root_vars.get("ADMIN_PASSWORD")
```

### Init seed vars (1Password)
```python
from libs.env import OpSecrets

init = OpSecrets()  # defaults to init/env_vars in Infra2 vault
vps_host = init.get("VPS_HOST")
```

### Deployer-based tasks
```python
from libs.deploy.deployer import Deployer, make_tasks
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
- `DEPLOY_ENV` selects the Dokploy Environment; env-scoped values (e.g. `DATA_PATH`, `ENV_SUFFIX`) should live in Dokploy Environment or CLI env when needed.
- Public domains follow `{subdomain}{ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}` where `ENV_DOMAIN_SUFFIX` is `""` for production and `"-<env>"` for non-prod; `ENV` must not include `-` or `/` (use `_`, which is converted to `-` in domains).
- `project`/`env`/`service` identifiers must not include `-` or `/` to keep `{project}/{env}/{service}` paths unambiguous.
- `ENV_SUFFIX` is opt-in and only used when explicitly set.
- `check_service()` uses the `CONTAINERS` mapping; SigNoz runs as `platform-signoz`.
- Non-production requires `DATA_PATH` or `ENV_SUFFIX` unless `ALLOW_SHARED_DATA_PATH=1` is set.
- `DokployClient.update_compose_env()` parses basic `KEY=VALUE` lines only (no quoted/escaped/multiline values).
- Dokploy deployment proof uses `deployment.allByCompose` before falling back to embedded compose snapshots.
- Deployer identity has two planes: runtime `IAC_CONFIG_HASH` for idempotence, and versioned secret-free `IAC_SOURCE_CONFIG_HASH` plus exact `IAC_DEPLOY_REF` for release provenance.
- Operational service identity is a third, metadata-only plane rendered by `service_identity.py`: registry-owned `service_id`/environment/component maps consistently to `INFRA_*`, OTEL resources, Docker labels and alert labels. It does not enter config hashes; missing/stale identity triggers one reconcile and post-deploy proof.
- `service_registry.py` resolves Dokploy project/compose and legacy Docker container coordinates. Ambiguous or unknown runtime objects remain `infra/unregistered`; callers must not guess.
- Dokploy API errors include method + endpoint context via `httpx` exceptions.
- Production App requests use read-only GitHub API metadata to bind approved source/staging workflows and the merged review commit to the requested source SHA.
- Infra contract and filesystem-discovery tests exclude `repos/`; workspace submodules own their own workflows and invariants.
- Workflow contract tests enforce repository-wide minimum majors for official JavaScript Actions so new workflows cannot reintroduce unsupported runtimes.
- `discover_services()` returns Invoke's CLI-normalized task names: service underscores become dashes (for example, `truealpha/data_engine` maps to `ta-data-engine.sync`), with a regression test against Invoke's `Collection.task_names` API.
- `VaultSecrets` reads `VAULT_ROOT_TOKEN` and `VAULT_ADDR` (or falls back to `https://vault.$INTERNAL_DOMAIN`).

## References

- **文档索引**: [docs/README.md](../docs/README.md)
- **Project Portfolio**: [docs/project/README.md](../docs/project/README.md)
- **AI 行为准则**: [AGENTS.md](../AGENTS.md)
- **SSOT**: [docs/ssot/platform.automation.md](../docs/ssot/platform.automation.md)
- **Core**: [docs/ssot/core.md](../docs/ssot/core.md)
- **Platform**: [platform/README.md](../platform/README.md)
