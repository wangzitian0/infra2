# Infra2 Internal Libraries (`libs/`)

> **Purpose**: Internal domain packages, deployment backends, and platform clients used by deploy scripts, CLI tools, and background workers.
> Stable cross-repository contracts live in [`infra2-sdk`](https://github.com/wangzitian0/infra2-sdk) and are imported directly.
> The infra2 release pin is `v2.5.0`; adoption and equality are guarded by `libs/tests/test_sdk_contract_adoption.py`.

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
| [`deploy/dokploy_client.py`](./deploy/dokploy_client.py) | Dokploy REST API wrapper (`libs/dokploy.py` is its shim) | `DokployClient`, `get_dokploy()` |
| [`observability_dashboards.py`](./observability_dashboards.py) | SigNoz alert rules and dashboards loader. It stays here: moving it triggers apply-observability.yml, which needs owner approval (#1059 phase 2). | `load_alert_definitions()`, `render_alert_payloads()`, `require_rule_channel()` |
| [`console.py`](./console.py) | Rich CLI formatting and header blocks | `header()`, `success()`, `error()`, `prompt_action()` |
| [`common.py`](./common.py) | Environment derivation re-export and operator health check helper | `get_env()`, `check_service()` |

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
| `libs/service_identity.py` | `libs.core.service_identity` | `ServiceIdentity`, `DOCKER_LABEL_PREFIX`, `MANAGED_BY` | Frozen Shim |
| `libs/env.py` | `libs.security.store` | `OpSecrets`, `VaultSecrets`, `get_secrets`, `generate_password`, `verify_vault_token` | Frozen Shim |
| `libs/service_registry.py` | `libs.core.registry` | `service_attrs`, `ServiceMeta`, `all_services`, `resolve_container_host` | Frozen Shim |
| `libs/service_facets.py` | `libs.core.facets` | `ProbeFacet`, `PublicRouteFacet`, `SignalFacet`, `BackupFacet`, `Exemption` | Frozen Shim |
| `libs/secrets_registry.py` | `libs.security.registry` | `SERVICES`, `Service`, `lookup`, `merged_manifest`, `store_keys` | Frozen Shim |
| `libs/deploy_dependencies.py` | `libs.deploy.dependencies` | `extra_dependency_globs`, `service_key_from_path` | Frozen Shim |
| `libs/deploy_queue.py` | `libs.deploy.queue` | `deployment_start_epoch`, `find_stuck_deploys` | Frozen Shim |
| `libs/deploy_env_config.py` | `libs.deploy.env_config` | `app_compose_env_config`, `preview_service_config`, `otel_env` | Frozen Shim |
| `libs/deploy_contract.py` | `libs.deploy.contract` | `service_spec`, `ServiceSpec`, `deploy_type_spec` | Frozen Shim |
| `libs/dokploy.py` | `libs.deploy.dokploy_client` | `DokployClient`, `get_dokploy`, `ensure_project` | Frozen Shim |
| `libs/page_dedup.py` | `libs.observability.page_dedup` | `dedup_page`, `resolve_page_state`, `decide`, `Finding` | Frozen Shim |
| `libs/availability_ledger.py` | `libs.observability.ledger` | `summarize_ledger`, `outage_intervals`, `build_report_message` | Frozen Shim |
| `libs/watchdog_signal_entries.py` | `libs.observability.signal_entries` | `render_internal_signal_entries` | Frozen Shim |
| `libs/harness_manifest.py` | `libs.core.harness.manifest` | `load_manifest`, `validate_manifest`, `check_workspace` | Frozen Shim |
| `libs/harness_status.py` | `libs.core.harness.status` | `workspace_status`, `repository_status` | Frozen Shim |
| `libs/harness_sweep.py` | `libs.core.harness.sweep` | `sweep`, `sweep_once`, `watch` | Frozen Shim |
| `libs/iac_runner_client.py` | `libs.deploy.iac_runner_client` | `trigger_platform_deploy`, `poll_platform_deploy_status` | Frozen Shim |
| `libs/release_markers.py` | `libs.deploy.release_markers` | `newest_release_tag`, `production_marker`, `marker_status` | Frozen Shim |
| `libs/app_deploy_request.py` | `libs.deploy.app_deploy_request` | `verify_production_evidence`, `validate_request_authority` | Frozen Shim |
| `libs/coverage_regression.py` | `libs.gate.coverage_regression` | `check_no_regression`, `load_baseline`, `read_coverage_summary` | Frozen Shim |
| `libs/vault_self_refresh_audit.py` | `libs.security.vault_self_refresh_audit` | `load_inventory`, `VaultService`, `CheckResult` | Frozen Shim |
| `libs/vault_tokens.py` | `libs.security.vault_tokens` | `VaultTokenTarget`, `policy_name`, `normalize_selector` | Frozen Shim |
| `libs/common.py` | — (re-exports `libs.core.environ`, and holds `check_service`) | — | Not a shim |
| `libs/console.py` | — (holds its own implementation) | — | Not a shim |

### The two that are not shims

- `libs/common.py` re-exports `libs.core.environ` and keeps one function of its own,
  `check_service`. That function is an operator task helper: it runs a health command
  over SSH and prints through `libs.console`, which a domain package must not import.
- `libs/console.py` holds its own implementation. `tools/pr_merge_gate.py` imports it, so
  it is in the merge gate's self-governing closure; moving it is a separate,
  owner-approved change. `libs/deploy/deployer.py` and `libs/deploy/promote.py` import
  the decoupled copy `libs/deploy/console.py`, so the import-boundary debt ledger is
  empty. `test_deploy_console_code_equals_flat_console_code` fails when the two copies
  differ in code.

The guard asserts that both are *not* structurally shims: migrate one and the table must
move with it.

`libs.security` imports without infra2-sdk (#847): `libs/security/__init__.py` loads
`supply` / `prune` lazily (PEP 562), so only touching an SDK-backed name needs the wheel;
`libs/tests/test_sdk_free_import_surface.py` pins it. `libs/security/store.py` (behind
the `libs/env.py` shim) guards its own SDK import so minimal GitHub Actions jobs can
use `verify_vault_token` / `generate_password`; its guards (`libs/tests/test_env.py::TestWithoutTheSdk`,
`test_secrets_registry.py::test_the_registry_table_is_readable_without_the_sdk` and
`test_workflow_runtime_deps.py::test_a_job_can_import_what_it_runs`) must keep passing
unchanged.

### Import boundaries

`libs/tests/test_import_boundaries.py` walks the AST of `libs/**` and fails on a domain
package importing a flat `libs/<name>.py`, or on anything under `libs/` importing
`tools`, `platform` or `bootstrap`. Violations that predate the guard (#955) sit in a
shrink-only debt ledger in that file: a new violation fails, and so does a ledger entry
whose import no longer exists.

---

## 🚀 Recommended Usage Patterns

### 1. Service Registry & Environment (`libs.core`)
```python
from libs.core.environ import get_env, with_env_suffix
from libs.core.registry import service_attrs

meta = service_attrs()["platform/postgres"]  # ServiceMeta, read from its deploy.py
print(meta.compose_path, meta.prod_only, [p.name for p in meta.probes])
container = with_env_suffix("platform-postgres", get_env())  # needs DEPLOY_ENV or INFRA_ENVIRONMENT; "-staging" outside prod
```

### 2. Secret Supply & Vault Access (`libs.security`)
```python
from libs.security import VaultSecrets, apply_secret_supply
from libs.security.registry import lookup

service = lookup("platform", "alerting")  # the secrets row, with its manifests
report = apply_secret_supply(service, "staging")

password = VaultSecrets(path="platform/staging/postgres").get("POSTGRES_PASSWORD")
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
