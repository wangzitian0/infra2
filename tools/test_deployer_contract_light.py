#!/usr/bin/env python3
"""Lightweight pre-flight tests for deployer contracts (config hash & service discovery).

Runs fast in CI and local dev pre-commit without spinning up full pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.deploy.deployer import (  # noqa: E402
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


def main() -> int:
    test_config_hash_idempotency()
    test_service_discovery()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
