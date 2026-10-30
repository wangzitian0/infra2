# Infra2 Internal Libraries (`common/`)

> **Purpose**: Internal domain packages, deployment backends, and platform clients used by deploy scripts, CLI tools, and background workers.
> Stable cross-repository contracts live in [`infra2-sdk`](https://github.com/wangzitian0/infra2-sdk) and are imported directly.
> The `infra2-sdk` wheel that `uv.lock` locks is the release pin. `common/tests/test_sdk_contract_adoption.py` guards adoption.

---

## 🏛️ Architecture & Domain Packages (SSOT)

Infrastructure capabilities are organized into 6 cohesive domain packages. Each package is self-contained with its own localized `README.md`, domain entities, invariants, and tests:

| Domain Package | SSOT Role | Local Documentation | Key Capabilities & APIs |
|----------------|-----------|---------------------|--------------------------|
| [`core/`](./core/README.md) | **Domain Entities & Environment** | [core/README.md](./core/README.md) | Single immutable `ServiceMeta` entity (`service_attrs`), typed `DeploymentEnvironment`, environment derivation (`with_env_suffix`), estate constants |
| [`security/`](./security/README.md) | **Secret Supply & Vault Lifecycle** | [security/README.md](./security/README.md) | Vault KV & 1Password resolution (`VaultSecrets`), deploy-time secret supply pipeline (`apply_secret_supply`), token generation, orphan key pruning |
| [`backup/`](./backup/README.md) | **Disaster Recovery & Rehearsal** | [backup/README.md](./backup/README.md) | Backup inventory discovery (`load_backup_inventory`), manifest verification, restore rehearsal specifications (`RehearsalSpecification`), sandboxed rehearsal execution |
| [`observability/`](./observability/README.md) | **Probes, Triage & Watchdogs** | [observability/README.md](./observability/README.md) | In-band health probes (`ProbeSpec`, `run_probes`), container breakdown log analysis (`analyze_container_logs`), watchdog issue trail reconciliation, alerting sidecar resident watchers |
| [`deploy/`](./deploy/README.md) | **Deployment Engine & Promotion** | [deploy/README.md](./deploy/README.md) | Unified `Deployer` base class, fixed-environment promotion (`deploy()`), dynamic preview stack lifecycle (`up()`, `down()`), pre-deploy schema gate (`schema_gate`) |
| [`alerting/`](./alerting/README.md) | **SigNoz Alerting & Feishu Delivery** | [alerting/README.md](./alerting/README.md) | SigNoz/Alertmanager payload parsing (`PagerMessage`), Feishu card rendering (`build_feishu_card_payload`), rule builders (`build_log_alert_rule`), and webhook/app delivery |

---

## 🔌 Platform Clients & Standalone Modules

Modules in `common/` that provide direct integrations or operational clients:

| Module | Purpose | Key Exports |
|--------|---------|-------------|
| [`deploy/dokploy_client.py`](./deploy/dokploy_client.py) | Dokploy REST API wrapper | `DokployClient`, `get_dokploy()` |
| [`observability_dashboards.py`](./observability_dashboards.py) | SigNoz alert rules and dashboards loader. It stays here: moving it triggers apply-observability.yml, which needs owner approval (#1059 phase 2). | `load_alert_definitions()`, `render_alert_payloads()`, `require_rule_channel()` |
| [`console.py`](./console.py) | Rich CLI formatting and header blocks | `header()`, `success()`, `error()`, `prompt_action()` |
| [`common.py`](./common.py) | Operator SSH health check helper | `check_service()` |

---

## 🛡️ Backward-Compatibility Shims (PEP 484)

All 33 legacy compatibility shims are retired (Issue #1059).
Callers import domain packages directly.
No frozen compatibility shims remain in `common/`.

| Legacy Shim | Implementation Module | Re-exported Symbols | Status |
|-------------|-----------------------|---------------------|--------|
| `common/common.py` | — (holds its own function, `check_service`) | — | Not a shim |
| `common/console.py` | — (holds its own implementation) | — | Not a shim |

### The two that are not shims

- `common/common.py` holds one function, `check_service`. It re-exports nothing (#1164).
  Import the environment helpers from `common.core.environ`.
  `check_service` is an operator task helper: it runs a health command
  over SSH and prints through `common.console`.
  `test_no_libs_common_env_reexports.py` fails on any other use of `common.common`.
- `common/console.py` holds its own implementation. `tools/pr_merge_gate.py` imports it, so
  it is in the merge gate's self-governing closure; moving it is a separate,
  owner-approved change. `common.console` is a pure leaf module with zero internal repo
  imports, enforced by `test_leaf_flat_modules_have_no_repo_imports` in
  `common/tests/test_import_boundaries.py`.

The guard asserts that both are *not* structurally shims: migrate one and the table must
move with it.

`common.security` imports without infra2-sdk (#847): `common/security/__init__.py` loads
`store`, `supply`, and `prune` lazily (PEP 562), so only touching an SDK-backed name needs
the wheel; `common/tests/test_sdk_free_import_surface.py`,
`test_secrets_registry.py::test_the_registry_table_is_readable_without_the_sdk` and
`test_workflow_runtime_deps.py::test_a_job_can_import_what_it_runs` pin this contract.

### Import boundaries

`common/tests/test_import_boundaries.py` walks the AST of `common/**` and fails on a domain
package importing a flat `common/<name>.py`, or on anything under `common/` importing
`tools`, `platform` or `bootstrap`. Violations that predate the guard (#955) sit in a
shrink-only debt ledger in that file: a new violation fails, and so does a ledger entry
whose import no longer exists.

---

## 🚀 Recommended Usage Patterns

### 1. Service Registry & Environment (`common.core`)
```python
from common.core.environ import get_env, with_env_suffix
from common.core.registry import service_attrs

meta = service_attrs()["platform/postgres"]  # ServiceMeta, read from its deploy.py
print(meta.compose_path, meta.prod_only, [p.name for p in meta.probes])
container = with_env_suffix("platform-postgres", get_env())  # needs DEPLOY_ENV or INFRA_ENVIRONMENT; "-staging" outside prod
```

### 2. Secret Supply & Vault Access (`common.security`)
```python
from common.security import VaultSecrets, apply_secret_supply
from common.security.registry import lookup

service = lookup("platform", "alerting")  # the secrets row, with its manifests
report = apply_secret_supply(service, "staging")

password = VaultSecrets(path="platform/staging/postgres").get("POSTGRES_PASSWORD")
```

### 3. Disaster Recovery & Rehearsal (`common.backup`)
```python
from pathlib import Path
from common.backup import (
    RehearsalSpecification,
    create_rehearsal_plan,
    execute_rehearsal,
    load_backup_inventory,
)

entry = next(e for e in load_backup_inventory() if "pg" in e.method)
spec = RehearsalSpecification(
    entry=entry,
    artifact={"remote_uri": "local:/tmp/rehearsal.dump"},
    archive_path=Path("/tmp/rehearsal.dump"),
    target_container="test-rehearsal-postgres",
    pg_user="postgres",
    database="postgres",
    invariant_sql=("SELECT 1;",),
)
plan = create_rehearsal_plan(spec)
result = execute_rehearsal(plan)
```

### 4. Observability Probes & Log Triage (`common.observability`)
```python
from common.observability import ProbeSpec, analyze_container_logs, run_probes

spec = ProbeSpec(name="pg-tcp", kind="tcp", target="platform-postgres:5432")
results = run_probes([spec])

verdict = analyze_container_logs("platform-alerting", "ERROR: connection refused")
print(f"Cause: {verdict.cause} ({verdict.detail})")
```

### 5. Deployment & Task Generation (`common.deploy`)
```python
from common.deploy.deployer import Deployer, make_tasks

class CustomDeployer(Deployer):
    service = "custom"
    compose_path = "platform/30.custom/compose.yaml"

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
