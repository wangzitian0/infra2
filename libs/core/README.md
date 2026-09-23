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
| `service.py` | Immutable service specification & registry loader | `Service`, `load_service_registry()`, `get_service()` |
| `environ.py` | Environment enum, suffix derivation, and validation | `DeploymentEnvironment`, `get_environment()`, `with_env_suffix()` |
| `constants.py` | Estate-wide constants and Docker labels | `DEPLOYMENT_ENV_PRODUCTION`, `DEPLOYMENT_ENV_STAGING`, `DEPLOYMENT_ENV_PREVIEW`, `DOCKER_LABEL_PREFIX`, `IDENTITY_SCHEMA_VERSION`, `MANAGED_BY`, `REPO_ROOT` |

## Usage Examples

### Loading Services from Registry
```python
from libs.core import get_service, load_service_registry

# Retrieve a specific service by ID
service = get_service("platform/postgres")
print(service.id, service.subdomain, service.compose_file)

# Iterate across all registered services
registry = load_service_registry()
for s in registry:
    print(s.id, s.deployer_class)
```

### Working with Deployment Environments
```python
from libs.core import DeploymentEnvironment, with_env_suffix

env = DeploymentEnvironment.from_str("staging")
assert env.is_staging
assert not env.is_production

# Compute container/domain suffix
suffix = with_env_suffix(env)  # "-staging" for staging, "" for production
```

## Invariants & Design Principles

- **Immutability**: `Service` is a `@dataclass(frozen=True)` to prevent runtime attribute mutation.
- **Fail-Closed Resolution**: `get_service()` raises `KeyError` when an unregistered service ID is requested.
- **Single Source of Truth**: Service definitions are parsed from compose configurations and facets, not hardcoded across multiple files.
- **Guards & Tests**: Covered by `libs/tests/test_service_registry.py` and `libs/tests/test_common.py`.
