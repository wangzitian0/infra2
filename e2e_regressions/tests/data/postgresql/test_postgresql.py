"""
PostgreSQL database tests.

Tests PostgreSQL connectivity when credentials are provided.
"""
import pytest
import os


def _get_db_env():
    host = os.getenv("PG_HOST") or os.getenv("DB_HOST")
    password = os.getenv("PG_PASS") or os.getenv("DB_PASSWORD")
    port = os.getenv("PG_PORT") or os.getenv("DB_PORT", "5432")
    user = os.getenv("PG_USER") or os.getenv("DB_USER", "postgres")
    database = os.getenv("PG_DB") or os.getenv("DB_NAME", "postgres")
    return host, password, port, user, database


@pytest.mark.database
async def test_postgresql_connect():
    """Test basic PostgreSQL connection."""
    db_host, db_password, db_port, db_user, db_name = _get_db_env()

    if not all([db_host, db_password]):
        pytest.skip("DB credentials not configured")

    import asyncpg
    conn = await asyncpg.connect(
        host=db_host,
        port=int(db_port),
        user=db_user,
        password=db_password,
        database=db_name,
        timeout=10.0,
    )

    result = await conn.fetchval("SELECT 1")
    assert result == 1
    await conn.close()


@pytest.mark.database
async def test_postgresql_version():
    """Verify PostgreSQL version."""
    db_host, db_password, db_port, db_user, db_name = _get_db_env()

    if not all([db_host, db_password]):
        pytest.skip("DB credentials not configured")

    import asyncpg
    conn = await asyncpg.connect(
        host=db_host,
        port=int(db_port),
        user=db_user,
        password=db_password,
        database=db_name,
    )

    version = await conn.fetchval("SELECT version()")
    assert "PostgreSQL" in version
    await conn.close()
