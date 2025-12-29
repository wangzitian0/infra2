"""
Bootstrap Storage Layer Tests.

Validates compose volume mappings and optional DB connectivity.
"""
import pathlib
import pytest
from conftest import TestConfig


ROOT = pathlib.Path(__file__).parent.parent.parent.parent.parent


def _read(path: pathlib.Path) -> str:
    return path.read_text()


def _assert_contains(path: pathlib.Path, needle: str, label: str):
    assert path.exists(), f"{label} file missing: {path}"
    content = _read(path)
    assert needle in content, f"{label} should include '{needle}'"


@pytest.mark.smoke
@pytest.mark.bootstrap
async def test_bootstrap_data_paths_defined():
    """Verify bootstrap services mount /data paths."""
    _assert_contains(ROOT / "bootstrap" / "04.1password" / "compose.yaml", "/data/bootstrap/1password", "1Password compose")
    _assert_contains(ROOT / "bootstrap" / "05.vault" / "compose.yaml", "/data/bootstrap/vault", "Vault compose")


@pytest.mark.bootstrap
async def test_platform_data_paths_defined():
    """Verify platform services mount /data paths."""
    _assert_contains(ROOT / "platform" / "01.postgres" / "compose.yaml", "/data/platform/postgres", "Postgres compose")
    _assert_contains(ROOT / "platform" / "02.redis" / "compose.yaml", "/data/platform/redis", "Redis compose")
    _assert_contains(ROOT / "platform" / "10.authentik" / "compose.yaml", "/data/platform/authentik", "Authentik compose")


@pytest.mark.database
async def test_platform_pg_accessible(config: TestConfig):
    """Verify platform PostgreSQL is accessible if credentials provided."""
    if not config.PLATFORM_DB_HOST or not config.PLATFORM_DB_PASSWORD:
        pytest.skip("Platform DB credentials not configured")

    try:
        import asyncpg
    except ImportError:
        pytest.skip("asyncpg not installed")

    conn = await asyncpg.connect(
        host=config.PLATFORM_DB_HOST,
        port=int(config.PLATFORM_DB_PORT),
        user=config.PLATFORM_DB_USER,
        password=config.PLATFORM_DB_PASSWORD,
        timeout=10.0,
    )
    result = await conn.fetchval("SELECT 1")
    assert result == 1
    await conn.close()
