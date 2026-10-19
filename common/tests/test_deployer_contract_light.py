#!/usr/bin/env python3
"""Lightweight pre-flight tests for deployer contracts (config hash & service discovery).

Runs fast in CI and local dev pre-commit without spinning up full pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.deploy.deployer import (  # noqa: E402
    _compute_config_hash,
    discover_services,
)


def test_config_hash_idempotency() -> None:
    # Test hash stability with different dict ordering
    hash1 = _compute_config_hash("test: true", {"A": "1", "B": "2"})
    hash2 = _compute_config_hash("test: true", {"B": "2", "A": "1"})
    assert hash1 == hash2, f"Hash not stable! {hash1} != {hash2}"

    # Test hash changes when content changes
    hash3 = _compute_config_hash("test: false", {"A": "1", "B": "2"})
    assert hash1 != hash3, "Hash should change with content"
    print("✅ Config hash idempotency verified")


def test_service_discovery() -> None:
    services = discover_services()
    print(f"Discovered {len(services)} services:")
    for key, task in sorted(services.items()):
        print(f"  {key} -> {task}")

    assert len(services) > 0, "Should discover at least one service"
    print("✅ Service discovery working")


def test_ensure_runner_sdk_compat_backfills_pre_v3_8_symbols() -> None:
    import importlib
    from tools.loader import _ensure_runner_sdk_compat

    secrets_mod = importlib.import_module("infra2_sdk.secrets")
    refs_mod = importlib.import_module("infra2_sdk.refs")
    saved_secrets = {
        name: getattr(secrets_mod, name)
        for name in ("VaultTokenTarget", "normalize_selector", "policy_name")
        if hasattr(secrets_mod, name)
    }
    saved_refs = {
        name: getattr(refs_mod, name)
        for name in ("assert_after_on_main", "resolve_branch_to_sha")
        if hasattr(refs_mod, name)
    }
    try:
        for name in saved_secrets:
            delattr(secrets_mod, name)
        for name in saved_refs:
            delattr(refs_mod, name)
        _ensure_runner_sdk_compat()
        target = secrets_mod.VaultTokenTarget(
            project="truealpha",
            service="data_engine",
            service_dir="truealpha/truealpha/20.data_engine",
            project_dir="truealpha/truealpha",
            dokploy_project="truealpha",
        )
        assert target.service == "data_engine"
        assert secrets_mod.normalize_selector("staging", label="env") == "staging"
        assert (
            secrets_mod.policy_name("truealpha", "staging", "data_engine")
            == "truealpha-staging-data_engine"
        )
        assert callable(refs_mod.assert_after_on_main)
        assert callable(refs_mod.resolve_branch_to_sha)
    finally:
        for name, val in saved_secrets.items():
            setattr(secrets_mod, name, val)
        for name, val in saved_refs.items():
            setattr(refs_mod, name, val)


def main() -> int:
    test_config_hash_idempotency()
    test_service_discovery()
    test_ensure_runner_sdk_compat_backfills_pre_v3_8_symbols()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
