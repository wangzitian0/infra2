"""
Disaster recovery tests.

Tests backup paths and recovery documentation.
"""
import pathlib
import pytest


ROOT = pathlib.Path(__file__).parent.parent.parent.parent.parent


def _assert_contains(path: pathlib.Path, needle: str, label: str):
    assert path.exists(), f"{label} should exist: {path}"
    content = path.read_text()
    assert needle in content, f"{label} should include '{needle}'"


@pytest.mark.ops
async def test_recovery_data_paths_defined():
    """Verify recovery-critical data paths are mounted."""
    _assert_contains(ROOT / "bootstrap" / "05.vault" / "compose.yaml", "/data/bootstrap/vault", "Vault compose")
    _assert_contains(ROOT / "platform" / "01.postgres" / "compose.yaml", "/data/platform/postgres", "Postgres compose")


@pytest.mark.ops
async def test_recovery_docs_exist():
    """Verify disaster recovery procedures are documented."""
    recovery_doc = ROOT / "docs" / "ssot" / "ops.recovery.md"

    assert recovery_doc.exists(), "Disaster recovery documentation should exist"
    content = recovery_doc.read_text()
    assert "恢复" in content or "Recovery" in content, "Documentation should contain recovery steps"
