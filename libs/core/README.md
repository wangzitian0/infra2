# Infra2 Core Domain Package (`libs/core`)

> **SSOT Domain**: Core immutable domain models, environment definitions, and invariant constants.

## Overview

`libs/core` provides the foundational abstractions for the entire `infra2` estate. It serves as the single source of truth for:
1. **The `Service` entity**: The single immutable dataclass representing deployed infrastructure services, applications, and their facets.
2. **Environment typing**: Strongly-typed `DeploymentEnvironment` with domain-suffix computation and stateful validation.
3. **Repository constants**: Shared domain names, portal mappings, Docker labels, and schema versions.

## Module Map

| Module | Role | Key Exports |
|--------|------|-------------|
| `environ.py` | Deployment environment of this process, suffixes, platform hosts (was `libs/common.py`, #955) | `get_env()`, `set_deploy_env()`, `with_env_suffix()`, `infra_domain()`, `service_domain()`, `DeploymentEnvironment` |
| `registry.py` | Service registry read from each Deployer by AST (was `libs/service_registry.py`) | `service_attrs()`, `ServiceMeta`, `all_services()`, `resolve_container_host()` |
| `facets.py` | Typed per-service facets a Deployer declares (was `libs/service_facets.py`) | `ProbeFacet`, `PublicRouteFacet`, `SignalFacet`, `BackupFacet`, `SecretsFacet`, `Exemption` |
| `constants.py` | Estate-wide constants and Docker labels | `DEPLOYMENT_ENV_PRODUCTION`, `DEPLOYMENT_ENV_STAGING`, `DEPLOYMENT_ENV_PREVIEW`, `DOCKER_LABEL_PREFIX`, `IDENTITY_SCHEMA_VERSION`, `MANAGED_BY`, `REPO_ROOT`, `GITHUB_OWNER` |
| `ci_spec.py` | CI testing hierarchy budgets and workflow parser | `GATE_WALL_CLOCK_BUDGET_S`, `read_workflow()`, `defanged_steps()`, `load_workflow()` |

## Usage Examples

### Reading the Service Registry
```python
from libs.core.registry import service_attrs

for service_id, meta in service_attrs().items():  # one ServiceMeta per deploy.py
    print(service_id, meta.subdomain, meta.prod_only)
```

### Working with the Deployment Environment
```python
from libs.core.environ import DeploymentEnvironment, get_env, with_env_suffix

staging = DeploymentEnvironment(name="staging", env_suffix="-staging")
assert staging.is_staging and not staging.is_production
assert with_env_suffix("platform-redis", staging) == "platform-redis-staging"
with_env_suffix("platform-redis", get_env())  # this process's environment: DEPLOY_ENV or INFRA_ENVIRONMENT, required
```

## Invariants & Design Principles

- **Single Source of Truth**: `ServiceMeta` is read by AST from each Deployer in `deploy.py`. The hand-written service tables (`libs.security.registry.SERVICES`, the app rows of `libs.deploy.contract.SERVICES`) are checked against it by `libs/tests/test_service_entity_projections.py` (#1023).
- **Immutability**: `ServiceMeta` and `DeploymentEnvironment` are `@dataclass(frozen=True)`.
- **No default environment** (#1039): `get_env()` raises `EnvironmentNotSetError` when neither `INFRA_ENVIRONMENT` nor `DEPLOY_ENV` holds a value. Production is chosen explicitly like any other environment. A caller that names its target passes it: `get_env("staging")`.
- **Guards & Tests**: Covered by `libs/tests/test_service_registry.py` and `libs/tests/test_common.py`.
