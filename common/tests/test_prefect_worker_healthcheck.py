"""Contract: platform/23.prefect runs prefect-server and prefect-services without a codeless default worker (#163/#961)."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "platform/23.prefect/compose.yaml"


def test_platform_prefect_drops_codeless_default_worker() -> None:
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    assert "prefect-worker" not in services
    assert {"prefect-server", "prefect-services"}.issubset(services)


def test_prefect_services_enables_flow_runs_db_vacuum_with_retention() -> None:
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    for svc_name in ("prefect-server", "prefect-services"):
        env = services[svc_name]["environment"]
        enabled = {
            part.strip()
            for part in str(
                env.get("PREFECT_SERVER_SERVICES_DB_VACUUM_ENABLED") or ""
            ).split(",")
            if part.strip()
        }
        assert {"events", "flow_runs"}.issubset(enabled), svc_name
        assert str(
            env.get("PREFECT_SERVER_SERVICES_DB_VACUUM_RETENTION_PERIOD") or ""
        ).strip(), svc_name
