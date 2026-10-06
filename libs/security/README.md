# Infra2 Security Domain Package (`libs/security`)

> **SSOT Domain**: Vault & 1Password secret resolution, token minting, deploy-time secret supply pipeline, and orphan pruning.

## Overview

`libs/security` implements the infrastructure secret lifecycle for `infra2`:
1. **Secret Storage Routing**: Thin wrappers over `infra2-sdk` backends (`VaultKvBackend`, `OnePasswordBackend`) to fetch runtime and human secrets.
2. **Deploy-Time Supply Pipeline**: Resolves missing or dynamic credentials via `apply_secret_supply()` prior to container start, mirroring runtime values into Vault.
3. **Orphan Pruning**: Audits Vault KV mounts to identify and safely remove keys that are no longer declared in any active service manifest.

## Module Map

| Module | Role | Key Exports |
|--------|------|-------------|
| `store.py` | 1Password and Vault stores, token helpers (was `libs/security/store.py`, #955) | `OpSecrets`, `VaultSecrets`, `get_secrets()`, `verify_vault_token()`, `generate_secret_token()`, `resolve_vault_token()` |
| `registry.py` | Which services take which secrets, and from which manifests (was `libs/security/registry.py`) | `SERVICES`, `Service`, `lookup()`, `merged_manifest()`, `store_keys()` |
| `app_manifests.py` | App environment manifests read at the pinned submodule commits (was `libs/app_manifests.py`) | `fetch_missing()`, `CACHE_DIR` |
| `supply.py` | Manifest secret supply pipeline | `apply_secret_supply()`, `create_secrets_resolver()`, `SupplyReport` |
| `prune.py` | Orphan KV key detection & cleanup | `prune_orphan_secrets()` |

`import libs.security` needs no infra2-sdk: `supply.py` / `prune.py` load lazily (PEP 562) on first access of an SDK-backed name, which then raises the SDK's `ImportError` if the wheel is absent (#847; pinned by `libs/tests/test_sdk_free_import_surface.py`).

## Usage Examples

### Applying Secret Supply during Deployment
```python
from libs.security import apply_secret_supply
from libs.security.registry import lookup

service = lookup("platform", "alerting")
# Apply manifest to Vault: generates runtime passwords and copies 1Password human values
report = apply_secret_supply(service, "staging")
print(f"Supplied: {report.changed}, Missing: {report.missing}")
```

### Resolving Tokens and Accessing Vault
```python
from libs.security import resolve_vault_token, VaultSecrets

token = resolve_vault_token()
client = VaultSecrets(path="platform/staging/postgres", token=token)
secret_val = client.get("POSTGRES_PASSWORD")
```

### Pruning Orphan Secrets
```python
from libs.security import prune_orphan_secrets

# Safe dry-run prune of undeclared Vault paths
orphans = prune_orphan_secrets(dry_run=True)
for path in orphans:
    print(f"Undeclared orphan: {path}")
```

## Security Invariants

- **Zero Cleartext Logging**: Exception traces and debug logs redact authenticated URLs, passwords, and tokens.
- **1Password as Static Root**: Human-entered secrets originate in 1Password; Vault is strictly for runtime-generated and cached secrets.
- **Fail-Closed on Unmet Secrets**: Deployments abort immediately if any non-optional manifest key cannot be resolved.
- **Guards & Tests**: Covered by `libs/tests/test_secrets_supply.py`, `libs/tests/test_secrets_prune.py`, and `libs/tests/test_vault_tokens.py`.
