"""
PostgreSQL database tests.

Tests PostgreSQL connectivity when credentials are provided.
"""

import pytest
from conftest import TestConfig


@pytest.mark.database
async def test_postgresql_connect(config: TestConfig):
    """Test basic PostgreSQL connection."""
    if not config.PLATFORM_DB_HOST or not config.PLATFORM_DB_PASSWORD:
        pytest.skip("Platform DB credentials not configured")

    import asyncpg

    conn = await asyncpg.connect(
        host=config.PLATFORM_DB_HOST,
        port=int(config.PLATFORM_DB_PORT),
        user=config.PLATFORM_DB_USER,
        password=config.PLATFORM_DB_PASSWORD,
        database=config.PLATFORM_DB_NAME,
        timeout=10.0,
    )

    result = await conn.fetchval("SELECT 1")
    assert result == 1
    await conn.close()


@pytest.mark.database
async def test_postgresql_version(config: TestConfig):
    """Verify PostgreSQL version."""
    if not config.PLATFORM_DB_HOST or not config.PLATFORM_DB_PASSWORD:
        pytest.skip("Platform DB credentials not configured")

    import asyncpg

    conn = await asyncpg.connect(
        host=config.PLATFORM_DB_HOST,
        port=int(config.PLATFORM_DB_PORT),
        user=config.PLATFORM_DB_USER,
        password=config.PLATFORM_DB_PASSWORD,
        database=config.PLATFORM_DB_NAME,
    )

    version = await conn.fetchval("SELECT version()")
    assert "PostgreSQL" in version
    await conn.close()
