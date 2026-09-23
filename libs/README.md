# Infra2 Internal Libraries

> **Purpose**: Internal libraries used by deploy scripts and CLI tools. Stable
> cross-repository contracts live in
> [`infra2-sdk`](https://github.com/wangzitian0/infra2-sdk) and are imported directly.
> The infra2 release pin is `v1.6.0`; adoption and Canary installation are
> equality-guarded by `libs/tests/test_sdk_contract_adoption.py`.

## At a Glance

- **Domain Packages (SSOT)**: Core infrastructure capabilities are organized into 4 cohesive domain packages:
  - `libs/core/`: The single immutable `Service` domain entity, typed `DeploymentEnvironment`, and invariant constants.
  - `libs/security/`: Vault / 1Password secret resolution, supply pipelines, and orphan prune.
  - `libs/backup/`: Backup verification, rehearsal specifications, and restore rehearsal execution.
  - `libs/observability/`: Infrastructure probes, breakdown analyzers, watchers, and watchdog issue reconciliation.
- `Deployer` + `make_tasks` standardize service deploy flows (via Dokploy API).
- `iac_runner_client` signs exact operation requests and polls by deployment ID, 2 s first and growing to 10 s (truealpha#860).
- `dokploy` wraps the Dokploy REST API for compose deployments.
- `console` helpers keep CLI output consistent (Rich).
- **Backward-Compatible Shims**: Legacy flat modules (`libs/env.py`, `libs/common.py`, `libs/secrets_supply.py`, `libs/service_registry.py`) provide PEP 484 re-exports to ensure zero breakage across existing consumers.

## Module Map

| Package / Module | Role | Key APIs |
|------------------|------|----------|
| `core/` | **SSOT** domain entities & environment | `Service`, `load_service_registry()`, `get_service()`, `DeploymentEnvironment`, `with_env_suffix()` |
| `security/` | **SSOT** secrets resolution & prune | `generate_secret_token()`, `resolve_vault_token()`, `apply_secret_supply()`, `prune_orphan_secrets()` |
| `backup/` | **SSOT** disaster recovery rehearsal | `RehearsalSpecification`, `create_rehearsal_plan()`, `execute_rehearsal()`, `load_backup_inventory()` |
| `observability/` | **SSOT** probes, diagnosis & alerts | `BreakdownVerdict`, `analyze_container_logs()`, `reconcile_watchdog_issues()`, `probe_postgres()`, `probe_s3()` |
| `deploy/deployer.py` | Deployment base class + task helpers | `Deployer`, `make_tasks()`, `Deployer.restart_dependents()` |
| `iac_runner_client.py` | Signed IaC Runner operation client | `trigger_platform_deploy()`, `poll_platform_deploy_status()` |
| `dokploy.py` | Dokploy API client | `DokployClient`, `get_dokploy()` |
| `deploy/preview.py` | Dynamic preview lifecycle | `up()`, `down()` |
| `app_deploy_request.py` | Fail-closed App request validation | `verify_production_evidence()`, `validate_request_authority()` |
| `harness_manifest.py` | Read-only workspace inventory | `load_manifest()`, `validate_manifest()` |
| `scheduler_peer_liveness.py` | Scheduler liveness check | `evaluate()`, `largest_gap()` |
| `watchdog_issue_trail.py` | Red ops-checks issue deduplication | `record_verdicts()`, `reconcile()` |
| `harness_status.py` | Repository pin/remote observation | `workspace_status()`, `repository_status()` |
| `harness_sweep.py` | Read-only orchestrator sweep | `sweep()`, `sweep_once()`, `watch()` |
| `env.py` (legacy shim) | Backward compatibility shim | `get_secrets`, `generate_password`, `vault_token` |
| `common.py` (legacy shim) | Shared environment helpers | `get_env()`, `validate_env()`, `check_service()` |
| `secrets_supply.py` (legacy shim) | Deploy-time secret supply shim | `apply()`, `resolver_for()` |

## Usage Patterns

### Secrets (SSOT-first)

`get_secrets()` routes to the backend that owns the value (`credential_type`):

| Type | Backend | Path Format |
|------|---------|-------------|
| `None` (default) | Vault | `secret/data/{project}/{env}/{service}` |
| `'app_vars'` | Vault | `secret/data/{project}/{env}/{service}` |
| `'bootstrap'` | 1Password | `{project}/{service}` (no env) |
| `'root_vars'` | 1Password | `{project}/{env}/{service}` |

```python
from libs.env import get_secrets

# Runtime values (Vault, default) — read here, written by the supply on deploy
db_pass = get_secrets("platform", "postgres", "production").get("POSTGRES_PASSWORD")

# Bootstrap credentials (1Password, no env layer)
dokploy_key = get_secrets("bootstrap", "dokploy", credential_type="bootstrap").get("DOKPLOY_API_KEY")

# Human-entered values (1Password, with env layer)
webhook = get_secrets("platform", "alerting", "production", credential_type="root_vars").get("FEISHU_WEBHOOK_URL")
```

Writes go through the manifest, not `secrets.set` (see
`docs/ssot/bootstrap.vars_and_secrets.md` §1.4):

```python
from libs import secrets_registry, secrets_supply

service = secrets_registry.lookup("platform", "alerting")
report = secrets_supply.apply(service, "staging")   # names only: report.changed / report.missing
```

`Deployer.apply_secret_supply` calls this in `pre_compose` for every registered service and
restarts the vault-agent and app containers when a value changed; `tools/secrets_reconcile.py`
re-checks every store daily (ops-checks). `invoke env.set` into Vault is break-glass only.

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
- `check_service()` uses the `CONTAINERS` mapping and quotes the local SSH shell and remote
  `docker exec ... sh -lc` shell independently, so nested Python/URL quotes remain intact;
  SigNoz runs as `platform-signoz`.
- Non-production requires `DATA_PATH` or `ENV_SUFFIX` unless `ALLOW_SHARED_DATA_PATH=1` is set.
- `DokployClient.update_compose_env()` parses basic `KEY=VALUE` lines only (no quoted/escaped/multiline values).
- Dokploy deployment proof uses `deployment.allByCompose` before falling back to embedded compose snapshots.
- Preview deployment proof snapshots deployment IDs before triggering, waits for that invocation's new terminal-good record, and then requires every service-configured public surface to serve the requested version. A stale old-stack 200 is never sufficient. The reserved canary is workflow-serialized; same-repository PRs bind a cloneable head branch to the exact head SHA; uncertain compose-create responses reconcile through the deterministic alias; and cleanup requires two consecutive absence observations.
- Deployer identity has two planes: runtime `IAC_CONFIG_HASH` for idempotence, and versioned secret-free `IAC_SOURCE_CONFIG_HASH` plus exact `IAC_DEPLOY_REF` for release provenance. A Deployer declaring `runtime_only_config_keys` must provide an explicit source builder that never reads its secret backend.
- Operational service identity is a third, metadata-only plane rendered by `service_identity.py`: registry-owned `service_id`/environment/component maps consistently to `INFRA_*`, OTEL resources, Docker labels and alert labels. It does not enter config hashes; missing/stale identity triggers one reconcile and post-deploy proof.
- `service_registry.py` resolves Dokploy project/compose and legacy Docker container coordinates. Ambiguous or unknown runtime objects remain `infra/unregistered`; callers must not guess.
- Dokploy API errors include method + endpoint context via `httpx` exceptions.
- Production App requests use read-only GitHub API metadata to bind approved source/staging workflows and the merged review commit to the requested source SHA; their infra `iac_ref` follows the latest explicit production marker rather than an unpromoted staging candidate, and marker absence fails closed as unknown production state.
- Infra contract and filesystem-discovery tests exclude `repos/`; workspace submodules own their own workflows and invariants.
- Workflow contract tests enforce repository-wide minimum majors for official JavaScript Actions so new workflows cannot reintroduce unsupported runtimes.
- `discover_services()` returns Invoke's CLI-normalized task names: service underscores become dashes (for example, `truealpha/data_engine` maps to `ta-data-engine.sync`), with a regression test against Invoke's `Collection.task_names` API.
- `VaultSecrets` reads `VAULT_TOKEN` (the runner's AppRole token; `VAULT_ROOT_TOKEN` is a transition alias for one release) and `VAULT_ADDR` (or falls back to `https://vault.$INTERNAL_DOMAIN`).

## References

- **文档索引**: [docs/README.md](../docs/README.md)
- **Project Portfolio**: [docs/project/README.md](../docs/project/README.md)
- **AI 行为准则**: [AGENTS.md](../AGENTS.md)
- **SSOT**: [docs/ssot/platform.automation.md](../docs/ssot/platform.automation.md)
- **Core**: [docs/ssot/core.md](../docs/ssot/core.md)
- **Platform**: [platform/README.md](../platform/README.md)
