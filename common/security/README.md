# Infra2 Security Domain Package (`common/security`)

> **SSOT Domain**: Vault & 1Password secret resolution, token minting, deploy-time secret supply pipeline, and orphan pruning.

## Overview

`common/security` implements the infrastructure secret lifecycle for `infra2`:
1. **Secret Storage Routing**: Thin wrappers over `infra2-sdk` backends (`VaultKvBackend`, `OnePasswordBackend`) to fetch runtime and human secrets.
2. **Deploy-Time Supply Pipeline**: Resolves missing or dynamic credentials via `apply_secret_supply()` prior to container start, mirroring runtime values into Vault.
3. **Orphan Pruning**: Audits Vault KV mounts to identify and safely remove keys that are no longer declared in any active service manifest.

## Module Map

| Module | Role | Key Exports |
|--------|------|-------------|
| `store.py` | 1Password and Vault stores, token helpers | `OpSecrets`, `VaultSecrets`, `get_secrets()`, `verify_vault_token()`, `generate_secret_token()`, `resolve_vault_token()` |
| `registry.py` | Which services take which secrets, and from which manifests. App stack baseline manifests live in each stack directory and merge with the deployed release manifest at deploy time (#1320). | `SERVICES`, `Service`, `lookup()`, `merged_manifest()`, `store_keys()` |
| `supply.py` | Manifest secret supply pipeline | `apply_secret_supply()`, `resolver_for()`, `SupplyReport` |
| `prune.py` | Orphan KV key detection & cleanup | `prune()`, `PruneReport`, `PrunePlan` |

`import common.security` needs no infra2-sdk: the package imports nothing. `store.py`, `supply.py` and `prune.py` need the SDK; import them directly (#847; pinned by `common/tests/test_sdk_free_import_surface.py`).

## Usage Examples

### Applying Secret Supply during Deployment
```python
from common.security.supply import apply_secret_supply
from common.security.registry import lookup

service = lookup("platform", "alerting")
# Apply manifest to Vault: generates runtime passwords and copies 1Password human values
report = apply_secret_supply(service, "staging")
print(f"Supplied: {report.changed}, Missing: {report.missing}, Notes: {report.notes}")
```

### Resolving Tokens and Accessing Vault
```python
from common.security.store import VaultSecrets, resolve_vault_token

token = resolve_vault_token()
client = VaultSecrets(path="platform/staging/postgres", token=token)
secret_val = client.get("POSTGRES_PASSWORD")
```

### Pruning Orphan Secrets
```python
from common.security.prune import prune

# Dry run (apply=False) of the undeclared Vault keys
report = prune(apply=False)
for plan in report.plans:
    if plan.orphans:
        print(f"{plan.path}: undeclared orphan keys {plan.orphans}")
```

## Security Invariants

- **Zero Cleartext Logging**: Exception traces and debug logs redact authenticated URLs, passwords, and tokens.
- **1Password as Static Root**: Human-entered secrets originate in 1Password; Vault is strictly for runtime-generated and cached secrets.
- **Fail-Closed on Unmet Secrets**: Deployments abort immediately if any non-optional manifest key cannot be resolved.
- **Guards & Tests**: Covered by `common/tests/test_secrets_supply.py`, `common/tests/test_secrets_prune.py`, and `common/tests/test_secrets_registry.py`.
