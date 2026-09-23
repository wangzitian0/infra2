# Infra2 Internal Libraries (`libs/`)

> **Purpose**: Internal domain packages, deployment backends, and platform clients used by deploy scripts, CLI tools, and background workers.
> Stable cross-repository contracts live in [`infra2-sdk`](https://github.com/wangzitian0/infra2-sdk) and are imported directly.
> The infra2 release pin is `v2.1.1`; adoption and equality are guarded by `libs/tests/test_sdk_contract_adoption.py`.

---

## 🏛️ Architecture & Domain Packages (SSOT)

Infrastructure capabilities are organized into 5 cohesive domain packages. Each package is self-contained with its own localized `README.md`, domain entities, invariants, and tests:

| Domain Package | SSOT Role | Local Documentation | Key Capabilities & APIs |
|----------------|-----------|---------------------|--------------------------|
| [`core/`](./core/README.md) | **Domain Entities & Environment** | [core/README.md](./core/README.md) | Single immutable `Service` entity, typed `DeploymentEnvironment`, environment derivation (`with_env_suffix`), estate constants |
| [`security/`](./security/README.md) | **Secret Supply & Vault Lifecycle** | [security/README.md](./security/README.md) | Vault KV & 1Password resolution (`VaultSecrets`), deploy-time secret supply pipeline (`apply_secret_supply`), token generation, orphan key pruning |
| [`backup/`](./backup/README.md) | **Disaster Recovery & Rehearsal** | [backup/README.md](./backup/README.md) | Backup inventory discovery (`load_backup_inventory`), manifest verification, restore rehearsal specifications (`RehearsalSpecification`), sandboxed rehearsal execution |
| [`observability/`](./observability/README.md) | **Probes, Triage & Watchdogs** | [observability/README.md](./observability/README.md) | In-band health probes (`ProbeSpec`, `execute_probe`), container breakdown log analysis (`analyze_container_logs`), watchdog issue trail reconciliation, alerting sidecar resident watchers |
| [`deploy/`](./deploy/README.md) | **Deployment Engine & Promotion** | [deploy/README.md](./deploy/README.md) | Unified `Deployer` base class, fixed-environment promotion (`deploy()`), dynamic preview stack lifecycle (`up()`, `down()`), pre-deploy schema gate (`schema_gate`) |

---

## 🔌 Platform Clients & Standalone Modules

Modules in `libs/` that provide direct integrations or operational clients:

| Module | Purpose | Key Exports |
|--------|---------|-------------|
| [`dokploy.py`](./dokploy.py) | Dokploy REST API wrapper | `DokployClient`, `get_dokploy()` |
| [`iac_runner_client.py`](./iac_runner_client.py) | Signed HMAC operation client for IaC Runner | `trigger_platform_deploy()`, `poll_platform_deploy_status()` |
| [`app_deploy_request.py`](./app_deploy_request.py) | Fail-closed App deploy request validation | `verify_production_evidence()`, `validate_request_authority()` |
| [`harness_manifest.py`](./harness_manifest.py) | Workspace inventory & autonomy boundary audit | `load_manifest()`, `validate_manifest()`, `check_workspace()` |
| [`harness_status.py`](./harness_status.py) | Git checkout pin/remote/release observation | `workspace_status()`, `repository_status()` |
| [`harness_sweep.py`](./harness_sweep.py) | Read-only orchestrator sweep for PRs & gates | `sweep()`, `sweep_once()`, `watch()` |
| [`console.py`](./console.py) | Rich CLI formatting and header blocks | `header()`, `success()`, `error()`, `prompt_action()` |
| [`availability_ledger.py`](./availability_ledger.py) | Pure availability ledger aggregation & uptime math | `aggregate_ledger()`, `calculate_uptime()` |

---

## 🛡️ Backward-Compatibility Shims (PEP 484)

Legacy flat modules in `libs/` are kept so existing CLI tools, workflows and
`docs/ssot/` snippets keep importing the path they always did. A **Frozen Shim** holds
**nothing but re-exports**: a docstring, one `from libs.<domain>.<module> import (...)`,
and `__all__`.

> [!IMPORTANT]
> **Shim Boundary Policy**: **Never add new business logic to a Frozen Shim.** All new
> features and refactorings go in the domain package (`libs.core`, `libs.security`,
> `libs.backup`, `libs.observability`, `libs.deploy`) and are imported from there.

This table is **not documentation, it is the contract**:
`libs/tests/test_frozen_shims.py` parses it and, for every `Frozen Shim` row, asserts
the module is structurally a re-export of exactly the module named here and that each
re-exported name `is` the same object on both paths. A row that claims more than the
code does fails the suite (#846: the table was written before the code moved, and for
six modules the code never followed).

| Legacy Shim | Implementation Module | Re-exported Symbols | Status |
|-------------|-----------------------|---------------------|--------|
| `libs/secrets_supply.py` | `libs.security.supply` | `apply`, `resolver_for`, `vault_backend`, `retrying_transport` | Frozen Shim |
| `libs/infra_probes.py` | `libs.observability.probes` | `execute_probe`, `run_probes`, `ProbeSpec`, `post_alert_bridge_payload` | Frozen Shim |
| `libs/watchdog_issue_trail.py` | `libs.observability.issue_trail` | `reconcile`, `record_verdicts`, `load_trail` | Frozen Shim |
| `libs/container_breakdown.py` | `libs.observability.breakdown` | `analyze_container_logs`, `build_breakdown_alert_payload` | Frozen Shim |
| `libs/container_breakdown_watch.py` | `libs.observability.watchers.breakdown_watch` | `BreakdownWatch`, `sweep`, `run_once` | Frozen Shim |
| `libs/backup_verification.py` | `libs.backup.verification` | `load_backup_inventory`, `verify_backup_manifest` | Frozen Shim |
| `libs/backup_restore.py` | `libs.backup.rehearsal` | `build_postgres_rehearsal_plan`, `run_postgres_restore_rehearsal` | Frozen Shim |
| `libs/env.py` | — (holds its own implementation) | — | Not a shim |
| `libs/common.py` | — (holds its own implementation) | — | Not a shim |
| `libs/service_registry.py` | — (holds its own implementation) | — | Not a shim |

### The three that are not shims yet

`libs/env.py` (293 lines), `libs/common.py` (394) and `libs/service_registry.py` (595)
are **implementation modules that a domain package imports**, not re-exports. They are
listed above so the table stays exhaustive, and the guard asserts they are *not*
structurally shims — migrate one and the table must move with it.

`libs/env.py` cannot simply move into `libs/security/store.py` as things stand:
`libs/security/__init__.py` eagerly imports `libs/security/supply.py`, whose
`from infra2_sdk.secrets import ...` is unconditional, so any `libs.security.*` import
requires the wheel. `libs/env.py` deliberately does not — it guards that import so
minimal GitHub Actions jobs can still use `verify_vault_token` / `generate_password`
(see its `try/except ModuleNotFoundError`, and the guards
`libs/tests/test_env.py::TestWithoutTheSdk`,
`test_secrets_registry.py::test_the_registry_table_is_readable_without_the_sdk` and
`test_workflow_runtime_deps.py::test_a_job_can_import_what_it_runs`). Moving it
requires first deciding whether `libs.security` may be imported without infra2-sdk.

---

## 🚀 Recommended Usage Patterns

### 1. Domain Entities & Registry (`libs.core`)
```python
from libs.core import get_service, load_service_registry, DeploymentEnvironment

service = get_service("platform/postgres")
registry = load_service_registry()
env = DeploymentEnvironment.from_str("staging")
```

### 2. Secret Supply & Vault Access (`libs.security`)
```python
from libs.core import get_service
from libs.security import apply_secret_supply, resolve_vault_token, VaultSecrets

service = get_service("platform/alerting")
report = apply_secret_supply(service, env="staging")

token = resolve_vault_token()
client = VaultSecrets(token=token)
secret = client.get("platform/postgres", "staging")
```

### 3. Disaster Recovery & Rehearsal (`libs.backup`)
```python
from libs.backup import RehearsalSpecification, create_rehearsal_plan, execute_rehearsal

spec = RehearsalSpecification(
    service_id="platform/postgres",
    database_name="postgres",
    target_container="test-rehearsal-postgres",
    target_port=15432,
)
plan = create_rehearsal_plan(spec, snapshot_date="latest")
result = execute_rehearsal(plan)
```

### 4. Observability Probes & Log Triage (`libs.observability`)
```python
from libs.observability import ProbeSpec, execute_probe, analyze_container_logs

spec = ProbeSpec(name="pg-tcp", kind="tcp", target="platform-postgres:5432")
result = execute_probe(spec)

verdict = analyze_container_logs("platform-alerting")
if verdict.is_broken:
    print(f"Container broken: {verdict.reason}")
```

### 5. Deployment & Task Generation (`libs.deploy`)
```python
from libs.deploy.deployer import Deployer, make_tasks
from libs.deploy.promote import deploy

class CustomDeployer(Deployer):
    service_id = "platform/custom"

tasks = make_tasks(CustomDeployer)
```

---

## 🔗 Related References

- **SSOT Core Architecture**: [`docs/ssot/core.md`](../docs/ssot/core.md)
- **SSOT Automation**: [`docs/ssot/platform.automation.md`](../docs/ssot/platform.automation.md)
- **SSOT Pipeline**: [`docs/ssot/ops.pipeline.md`](../docs/ssot/ops.pipeline.md)
- **SSOT Recovery**: [`docs/ssot/ops.recovery.md`](../docs/ssot/ops.recovery.md)
- **SSOT Secrets**: [`docs/ssot/bootstrap.vars_and_secrets.md`](../docs/ssot/bootstrap.vars_and_secrets.md)
- **AI Agent Guidelines**: [`AGENTS.md`](../AGENTS.md)
