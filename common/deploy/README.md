# Infra2 Deploy Domain Package (`common/deploy`)

> **SSOT Domain**: Unified deployment backends, Dokploy API integration, preview lifecycle, promotion pipelines, and schema validation gates.

## Overview

`common/deploy` contains the centralized deployment engines behind the `deploy_v2` interface (`tools.deploy_v2`):
1. **`Deployer` Base Abstraction**: The core engine driving service deploy tasks (`pre_compose`, `compose`, `post_compose`, `restart_dependents`), wired to Dokploy REST endpoints.
2. **Fixed-Environment Promotion (`promote.py`)**: Authoritative backend for `staging` and `production` rollouts with strict change-set auditing, provenance guards, and production safety gates (RL-DATA-1: `staging_validated` + `code_reviewed`).
3. **Dynamic Preview Lifecycle (`preview.py`)**: Manages isolated multi-alias preview deployments (`branch-<name>`, `pr-<N>`, `commit-<sha7>`, `tag-<slug>`) with trigger-bound terminal teardown.
4. **Pre-Deploy Schema Gate (`schema_gate.py`)**: Runs `python -m infra2_sdk.storage check-schema` in the app image against the live database before a deploy replaces containers. Exit 1 (blocked) and exit 3 (not evaluated) stop the deploy.

## Module Map

| Module | Role | Key Exports |
|--------|------|-------------|
| `deployer.py` | Platform & app deployment base class | `Deployer`, `make_tasks()`, `Deployer.apply_secret_supply()` |
| `stack_presets.py` | App PostgreSQL/Redis stack presets; facts come from `common/core/stack_preset_facts.py` | `PostgresStackDeployer`, `RedisStackDeployer`, `stack_tasks()` |
| `promote.py` | Staging & production promotion pipeline | `deploy()`, fixed environment orchestration |
| `preview.py` | Dynamic preview stack lifecycle | `up()`, `down()`, preview slot routing |
| `schema_gate.py` | Pre-deploy enum schema gate against live DB (`infra2_sdk.storage.check_schema` in the app image) | `SchemaCheckSpec`, `SchemaGateError`, `gate_applies()`, `run_schema_gate()` |
| `in_service.py` | Container health & liveness verification | `ContainerObservation`, `InServiceVerdict`, `expected_running_services()`, `parse_docker_ps()`, `in_service_verdict()`, `container_names()` |
| `rollout.py` | Dokploy rollout polling & terminal status | `RolloutError`, `RolloutResult`, `wait_for_deployment()`, `TERMINAL_SUCCESS_STATUSES` |
| `failure_snapshot.py` | Dokploy failure diagnosis & GitHub summary snapshot | `emit_failure_snapshot()`, `build_snapshot()`, `classify()` |
| `contract.py` | Deploy target contract per service | `service_spec()`, `ServiceSpec`, `validate_deploy_target()` |
| `env_config.py` | Compose environment for fixed and preview stacks | `app_compose_env_config()`, `preview_service_config()`, `otel_env()` |
| `queue.py` | Dokploy deploy-queue facts | `deployment_start_epoch()`, `find_stuck_deploys()` |
| `dependencies.py` | Deploy fan-out from `docs/ssot/deploy-dependencies.yaml` | `extra_dependency_globs()`, `service_key_from_path()` |
| `compose_lock.py` | In-process compose env write lock | `compose_write_lock()` |
| `dokploy_client.py` | Dokploy REST API client | `DokployClient`, `get_dokploy()`, `ensure_project()` |
| `host_hygiene_schedule.py` | Dokploy host-hygiene schedule provisioner (entry: `tools.host_hygiene_schedule`, scheduled automation) | `build_hygiene_script()`, `ensure_host_hygiene_schedule()`, `main()` |
| `facet_reconcile.py` | Daily compose_id, config-hash and DNS drift reconcile (entry: `tools.facet_reconcile`, scheduled automation) | `Section`, `run_all()`, `confirmed_finding_pairs()`, `main()` |
| `app_contract.py` | App manifest preflight, Vault template rendering, SDK hooks, and release evidence | `verify_app_manifests()`, `render_release_template()`, `execute_app_hook_slot()`, `query_serving_release()` |
| `release_markers.py` | Production release marker CRUD and lag guard CLI | `newest_release_tag()`, `production_marker()`, `marker_status()` |

## Usage Examples

### Implementing a Service Deployer
```python
from common.deploy.deployer import Deployer, make_tasks

class PostgresDeployer(Deployer):
    service = "postgres"
    compose_path = "platform/01.postgres/compose.yaml"

    def pre_compose(self, c) -> None:
        super().pre_compose(c)
        # Custom pre-flight initialization

# Generate Invoke tasks for CLI integration
tasks = make_tasks(PostgresDeployer)
```

### Triggering a Fixed-Environment Promotion
```python
from common.deploy.dokploy_client import get_dokploy
from common.deploy.promote import PromoteDeps, PromoteOptions, deploy

# Orchestrates staging promotion for an app release tag (CLI front door: tools.deploy_v2)
plan = deploy(
    "staging",
    "v1.1.96",
    options=PromoteOptions(service="finance_report/app", iac_ref="v1.1.96"),
    deps=PromoteDeps(client=get_dokploy(), domain="zitian.party"),
)
assert plan.env == "staging"
```

## Safety Invariants

- **Immutable Ref Enforcement**: Staging and production accept immutable release tags only (promote-not-rebuild).
- **RL-DATA-1 Deny-by-Default**: Production deployments fail closed unless both `--staging-validated` and `--code-reviewed` are explicitly proven.
- **Fail-Closed Schema Gate**: Any syntax defect, unbound variable, or schema violation blocks deployment before Dokploy receives payload.
- **Guards & Tests**: Covered by `common/tests/test_deploy_primitive.py`, `common/tests/test_preview_lifecycle.py`, and `common/tests/test_deploy_v2.py`.
