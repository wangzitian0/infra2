"""
Bootstrap structural tests for Dokploy-based deployment.

Ensures required compose files exist for bootstrap/platform services.
"""
import pathlib
import pytest


ROOT = pathlib.Path(__file__).parent.parent.parent.parent.parent


def _assert_file(path: pathlib.Path, label: str):
    assert path.exists(), f"{label} should exist at {path}"


@pytest.mark.bootstrap
async def test_bootstrap_compose_files_exist():
    """Verify bootstrap compose files exist."""
    _assert_file(ROOT / "bootstrap" / "04.1password" / "compose.yaml", "1Password compose")
    _assert_file(ROOT / "bootstrap" / "05.vault" / "compose.yaml", "Vault compose")
    _assert_file(ROOT / "bootstrap" / "05.vault" / "vault.hcl", "Vault config")


@pytest.mark.bootstrap
async def test_platform_compose_files_exist():
    """Verify platform compose files exist."""
    _assert_file(ROOT / "platform" / "01.postgres" / "compose.yaml", "Postgres compose")
    _assert_file(ROOT / "platform" / "02.redis" / "compose.yaml", "Redis compose")
    _assert_file(ROOT / "platform" / "10.authentik" / "compose.yaml", "Authentik compose")
