"""
Business PostgreSQL database tests.

Tests the main PostgreSQL database for business data.
"""
import pytest
import os


@pytest.mark.database
async def test_postgresql_connect():
    """Test basic PostgreSQL connection."""
    db_host = os.getenv("DB_HOST")
    db_password = os.getenv("DB_PASSWORD")
    
    if not all([db_host, db_password]):
        pytest.skip("DB credentials not configured")
    
    import asyncpg
    conn = await asyncpg.connect(
        host=db_host,
        port=int(os.getenv("DB_PORT", "5432")),
        user=os.getenv("DB_USER", "postgres"),
        password=db_password,
        timeout=10.0,
    )
    
    result = await conn.fetchval('SELECT 1')
    assert result == 1
    await conn.close()


@pytest.mark.database
async def test_postgresql_version():
    """Verify PostgreSQL version."""
    db_host = os.getenv("DB_HOST")
    db_password = os.getenv("DB_PASSWORD")
    
    if not all([db_host, db_password]):
        pytest.skip("DB credentials not configured")
    
    import asyncpg
    conn = await asyncpg.connect(
        host=db_host,
        port=int(os.getenv("DB_PORT", "5432")),
        user=os.getenv("DB_USER", "postgres"),
        password=db_password,
    )
    
    version = await conn.fetchval('SELECT version()')
    assert "PostgreSQL" in version
    await conn.close()

