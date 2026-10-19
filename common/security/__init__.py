"""Infra2 Security Domain Package."""

from __future__ import annotations

from typing import TYPE_CHECKING

# Importing this package must not require infra2-sdk (#847).
#
# `supply.py` and `prune.py` import `infra2_sdk.secrets` unconditionally, so an eager
# import here made every `common.security.*` import need the wheel -- including the ones a
# minimal GitHub Actions job makes only for `VaultSecrets` / `generate_secret_token`.
# Three guards pin that invariant at its source: `test_env.py::TestWithoutTheSdk`,
# `test_secrets_registry.py::test_the_registry_table_is_readable_without_the_sdk` (a real
# CI incident, run 34938044492) and `test_workflow_runtime_deps.py::
# test_a_job_can_import_what_it_runs`; `test_sdk_free_import_surface.py` pins it for this
# package.
#
# `store` has no SDK dependency, so it stays eager. The SDK-backed names resolve through
# a PEP 562 module-level `__getattr__`: `from common.security import apply_secret_supply`
# keeps working, but only that access loads `supply` / `prune`, and without the SDK it
# raises the SDK's own ImportError rather than returning a stub.
if TYPE_CHECKING:
    from common.security.prune import prune_orphan_secrets
    from common.security.store import (
        VaultSecrets,
        generate_secret_token,
        resolve_vault_token,
    )
    from common.security.supply import (
        SupplyReport,
        apply_secret_supply,
        create_secrets_resolver,
    )

# Names that need infra2-sdk; each maps to the submodule that defines it.
_SDK_BACKED: dict[str, str] = {
    "SupplyReport": "common.security.supply",
    "VaultSecrets": "common.security.store",
    "apply_secret_supply": "common.security.supply",
    "create_secrets_resolver": "common.security.supply",
    "generate_secret_token": "common.security.store",
    "prune_orphan_secrets": "common.security.prune",
    "resolve_vault_token": "common.security.store",
}

__all__ = [
    "SupplyReport",
    "VaultSecrets",
    "apply_secret_supply",
    "create_secrets_resolver",
    "generate_secret_token",
    "prune_orphan_secrets",
    "resolve_vault_token",
]


def __getattr__(name: str):
    if name not in _SDK_BACKED:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(_SDK_BACKED[name]), name)
    globals()[name] = value  # resolve once
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_SDK_BACKED))
