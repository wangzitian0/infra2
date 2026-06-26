"""Tests for the deployed-version SigNoz ingestion smoke (infra2-side #621 home)."""

from __future__ import annotations

import pytest

from tools import deploy_ingestion_smoke as smoke


def _counter(counts: dict[tuple[str, str | None], int]):
    """Fake count callable keyed by (data_source, version) inferred from the SQL."""

    def counter(query: str) -> int:
        kind = "logs" if "distributed_logs_v2" in query else "traces"
        version = None
        if "service.version" in query:
            # the version literal is the last quoted token in the WHERE clause
            version = query.split("service.version'] = '")[1].split("'")[0]
        return counts[(kind, version)]

    return counter


def test_passes_when_deployed_version_logs_and_traces_are_queryable() -> None:
    counter = _counter(
        {
            ("logs", None): 173,
            ("logs", "v0.1.20"): 173,
            ("traces", None): 1050,
            ("traces", "v0.1.20"): 1050,
        }
    )
    passed = smoke.verify_deploy_ingestion(
        clickhouse_url="http://clickhouse:8123",
        service_name="finance-report-backend",
        environment="production",
        expected_version="v0.1.20",
        counter=counter,
        sleeper=lambda _s: None,
    )
    assert any("logs ingested" in p and "v0.1.20" in p for p in passed)
    assert any("traces ingested" in p and "v0.1.20" in p for p in passed)


def test_fails_distinctly_on_zero_ingestion() -> None:
    counter = _counter(
        {
            ("logs", None): 0,
            ("logs", "v0.1.20"): 0,
            ("traces", None): 0,
            ("traces", "v0.1.20"): 0,
        }
    )
    with pytest.raises(smoke.IngestionSmokeError, match="zero logs"):
        smoke.verify_deploy_ingestion(
            clickhouse_url="http://clickhouse:8123",
            service_name="finance-report-backend",
            environment="production",
            expected_version="v0.1.20",
            counter=counter,
            poll_attempts=2,
            sleeper=lambda _s: None,
        )


def test_fails_distinctly_on_stale_image() -> None:
    counter = _counter(
        {
            ("logs", None): 200,
            ("logs", "v0.1.20"): 0,
            ("traces", None): 200,
            ("traces", "v0.1.20"): 200,
        }
    )
    with pytest.raises(smoke.IngestionSmokeError, match="stale image"):
        smoke.verify_deploy_ingestion(
            clickhouse_url="http://clickhouse:8123",
            service_name="finance-report-backend",
            environment="production",
            expected_version="v0.1.20",
            counter=counter,
            poll_attempts=2,
            sleeper=lambda _s: None,
        )


def test_fails_distinctly_on_absent_traces() -> None:
    counter = _counter(
        {
            ("logs", None): 200,
            ("logs", "v0.1.20"): 200,
            ("traces", None): 0,
            ("traces", "v0.1.20"): 0,
        }
    )
    with pytest.raises(smoke.IngestionSmokeError, match="zero traces"):
        smoke.verify_deploy_ingestion(
            clickhouse_url="http://clickhouse:8123",
            service_name="finance-report-backend",
            environment="production",
            expected_version="v0.1.20",
            counter=counter,
            poll_attempts=2,
            sleeper=lambda _s: None,
        )


def test_logs_query_uses_nanosecond_bound_and_resource_filters() -> None:
    q = smoke._logs_count_query(
        service_name="finance-report-backend",
        environment="staging",
        version="v0.1.20",
        window_minutes=15,
    )
    assert "distributed_logs_v2" in q
    assert "toUnixTimestamp64Nano(" in q  # avoids the DateTime64 overflow
    assert "resources_string['service.name'] = 'finance-report-backend'" in q
    assert "resources_string['deployment.environment'] = 'staging'" in q
    assert "resources_string['service.version'] = 'v0.1.20'" in q
