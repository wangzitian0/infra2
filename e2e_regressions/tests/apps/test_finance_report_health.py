"""
Finance Report application health checks.

Validates the app endpoint and dependency health for PR-test/staging/production.
"""

import pytest
import httpx
from conftest import TestConfig


def _health_url(config: TestConfig) -> str:
    return f"{config.FINANCE_REPORT_API_URL.rstrip('/')}/health"


@pytest.mark.smoke
@pytest.mark.app
async def test_finance_report_health_endpoint(config: TestConfig):
    """Verify Finance Report /api/health returns healthy with dependency checks."""
    async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
        response = await client.get(_health_url(config))
        assert response.status_code == 200, (
            f"Health check failed: {response.status_code}"
        )

        data = response.json()
        assert data.get("status") == "healthy", f"Health status is not healthy: {data}"

        checks = data.get("checks")
        assert isinstance(checks, dict), "Health response missing checks"
        assert checks.get("database") is True, "Database check should be healthy"
        assert checks.get("redis") is True, "Redis check should be healthy"
        assert checks.get("s3") is True, "S3 check should be healthy"


@pytest.mark.app
async def test_finance_report_frontend_available(config: TestConfig):
    """Verify Finance Report frontend is reachable."""
    async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
        response = await client.get(config.FINANCE_REPORT_URL)
        assert response.status_code < 500, f"Frontend returned {response.status_code}"
