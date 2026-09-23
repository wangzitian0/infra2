# Infra2 Deploy Domain Package (`libs/deploy`)

> **SSOT Domain**: Unified deployment backends, Dokploy API integration, preview lifecycle, promotion pipelines, and schema validation gates.

## Overview

`libs/deploy` contains the centralized deployment engines behind the `deploy_v2` interface (`tools.deploy_v2`):
1. **`Deployer` Base Abstraction**: The core engine driving service deploy tasks (`pre_compose`, `compose`, `post_compose`, `restart_dependents`), wired to Dokploy REST endpoints.
2. **Fixed-Environment Promotion (`promote.py`)**: Authoritative backend for `staging` and `production` rollouts with strict change-set auditing, provenance guards, and production safety gates (RL-DATA-1: `staging_validated` + `code_reviewed`).
3. **Dynamic Preview Lifecycle (`preview.py`)**: Manages isolated multi-alias preview deployments (`preview-main`, `preview-pr`, `preview-commit`, `preview-tag`) with trigger-bound terminal teardown.
4. **Pre-Deploy Schema Gate (`schema_gate.py`)**: Validates compose YAML syntax, environment mappings, and variable substitution completeness prior to applying changes.

## Module Map

| Module | Role | Key Exports |
|--------|------|-------------|
| `deployer.py` | Platform & app deployment base class | `Deployer`, `make_tasks()`, `Deployer.apply_secret_supply()` |
| `promote.py` | Staging & production promotion pipeline | `deploy()`, fixed environment orchestration |
| `preview.py` | Dynamic preview stack lifecycle | `up()`, `down()`, preview slot routing |
| `schema_gate.py` | Compose & environment schema validation | `validate_compose_schema()`, `assert_clean_environment()` |
| `in_service.py` | Container health & liveness verification | `assert_in_service()`, `wait_for_containers()` |
| `rollout.py` | Terminal rollout hooks & post-deploy checks | `verify_rollout()`, `terminal_summary()` |
| `failure_snapshot.py` | Dokploy failure diagnosis & GitHub summary snapshot | `emit_failure_snapshot()`, `build_snapshot()`, `classify()` |

## Usage Examples

### Implementing a Service Deployer
```python
from libs.deploy.deployer import Deployer, make_tasks

class PostgresDeployer(Deployer):
    service_id = "platform/postgres"

    def pre_compose(self, c, env: str) -> None:
        super().pre_compose(c, env)
        # Custom pre-flight initialization

# Generate Invoke tasks for CLI integration
tasks = make_tasks(PostgresDeployer)
```

### Triggering a Fixed-Environment Promotion
```python
from libs.deploy.promote import deploy

# Orchestrates staging promotion with immutable release tag
result = deploy(
    service_key="platform/alerting",
    deploy_type="staging",
    version_ref="v1.1.96",
    iac_ref="v1.1.96",
    domain="zitian.party",
)
assert result.returncode == 0
```

## Safety Invariants

- **Immutable Ref Enforcement**: Staging and production accept immutable release tags only (promote-not-rebuild).
- **RL-DATA-1 Deny-by-Default**: Production deployments fail closed unless both `--staging-validated` and `--code-reviewed` are explicitly proven.
- **Fail-Closed Schema Gate**: Any syntax defect, unbound variable, or schema violation blocks deployment before Dokploy receives payload.
- **Guards & Tests**: Covered by `libs/tests/test_deploy_primitive.py`, `libs/tests/test_preview_lifecycle.py`, and `libs/tests/test_promote.py`.
