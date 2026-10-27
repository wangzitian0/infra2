"""Tests for ClickHouse and Authentik resource bounds (#1190).

Ensures ClickHouse background schedule pools, memory caps, cache sizes,
async insert batching, and Authentik container memory limits adhere to
the resource stabilization specification.
"""

from __future__ import annotations

import json
from typing import Any
import xml.etree.ElementTree as ET
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_platform_clickhouse_config_bounds() -> None:
    path = ROOT / "platform/03.clickhouse/config.xml"
    assert path.exists()
    root = ET.fromstring(path.read_text(encoding="utf-8"))

    # Memory cap must exist and be <= 3.0 GiB (container mem_limit is 3584m)
    mem_elem = root.find("max_server_memory_usage")
    assert mem_elem is not None
    assert int(mem_elem.text) <= 3_000_000_000

    # Background schedule pool must be right-sized (<= 64 threads, down from default 512)
    pool_elem = root.find("background_schedule_pool_size")
    assert pool_elem is not None
    assert int(pool_elem.text) <= 64

    # Caches must be right-sized (<= 1 GiB each, down from 8.5 GiB and 5.3 GiB)
    uncompressed = root.find("uncompressed_cache_size")
    mark = root.find("mark_cache_size")
    assert uncompressed is not None and int(uncompressed.text) <= 1_073_741_824
    assert mark is not None and int(mark.text) <= 1_073_741_824


def test_platform_clickhouse_users_async_insert() -> None:
    path = ROOT / "platform/03.clickhouse/users.xml"
    assert path.exists()
    root = ET.fromstring(path.read_text(encoding="utf-8"))

    profile = root.find("profiles/default")
    assert profile is not None

    async_insert = profile.find("async_insert")
    wait_insert = profile.find("wait_for_async_insert")
    log_queries = profile.find("log_queries")
    log_threads = profile.find("log_query_threads")

    assert async_insert is not None and async_insert.text == "1"
    assert wait_insert is not None and wait_insert.text == "1"
    assert log_queries is not None and log_queries.text == "0"
    assert log_threads is not None and log_threads.text == "0"


def test_openpanel_clickhouse_bounds() -> None:
    config_path = ROOT / "platform/24.openpanel/clickhouse/clickhouse-config.xml"
    assert config_path.exists()
    cfg_root = ET.fromstring(config_path.read_text(encoding="utf-8"))

    pool_elem = cfg_root.find("background_schedule_pool_size")
    assert pool_elem is not None
    assert int(pool_elem.text) <= 64

    user_path = ROOT / "platform/24.openpanel/clickhouse/clickhouse-user-config.xml"
    assert user_path.exists()
    usr_root = ET.fromstring(user_path.read_text(encoding="utf-8"))

    profile = usr_root.find("profiles/default")
    assert profile is not None

    async_insert = profile.find("async_insert")
    wait_insert = profile.find("wait_for_async_insert")
    assert async_insert is not None and async_insert.text == "1"
    assert wait_insert is not None and wait_insert.text == "1"


def test_authentik_resource_and_healthcheck_limits() -> None:
    path = ROOT / "platform/10.authentik/compose.yaml"
    assert path.exists()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    services = data.get("services", {})

    server = services.get("server", {})
    worker = services.get("worker", {})

    assert server.get("mem_limit") == "768m"
    assert worker.get("mem_limit") == "512m"
    assert services.get("vault-agent", {}).get("mem_limit") == "128m"

    assert server.get("healthcheck", {}).get("interval") == "60s"
    assert worker.get("healthcheck", {}).get("interval") == "60s"


def test_openpanel_and_finance_report_resource_bounds() -> None:
    openpanel_path = ROOT / "platform/24.openpanel/compose.yaml"
    assert openpanel_path.exists()
    op_data = yaml.safe_load(openpanel_path.read_text(encoding="utf-8")) or {}
    op_services = op_data.get("services", {})

    assert op_services.get("vault-agent", {}).get("mem_limit") == "128m"
    for svc_name in ("op-api", "op-dashboard", "op-worker"):
        svc = op_services.get(svc_name, {})
        assert svc.get("mem_limit") == "512m"
        assert svc.get("init") is True

    fr_app_path = ROOT / "finance_report/finance_report/10.app/compose.yaml"
    assert fr_app_path.exists()
    fr_app_data = yaml.safe_load(fr_app_path.read_text(encoding="utf-8")) or {}
    fr_app_services = fr_app_data.get("services", {})

    assert fr_app_services.get("prefect-worker", {}).get("mem_limit") == "1g"
    assert (
        fr_app_services.get("backend", {}).get("healthcheck", {}).get("interval")
        == "30s"
    )

    fr_preview_path = ROOT / "finance_report/finance_report/preview/compose.yaml"
    assert fr_preview_path.exists()
    fr_preview_data = yaml.safe_load(fr_preview_path.read_text(encoding="utf-8")) or {}
    fr_preview_services = fr_preview_data.get("services", {})

    assert (
        fr_preview_services.get("backend", {}).get("healthcheck", {}).get("interval")
        == "30s"
    )

    tasks_text = (ROOT / "bootstrap/01.dokploy_install/tasks.py").read_text(
        encoding="utf-8"
    )
    assert "docker service update --init --env-add NODE_OPTIONS=" in tasks_text

    install_sh_text = (
        ROOT / "bootstrap/01.dokploy_install/install_dokploy.sh"
    ).read_text(encoding="utf-8")
    assert "docker service update --init --env-add NODE_OPTIONS=" in install_sh_text


def test_container_memory_limits_and_baseline_empty() -> None:
    """Verify compose memory limits are configured and baseline is empty."""
    from common.core.lint_compose_resource_limits import (
        BASELINE_PATH,
        main as lint_main,
    )

    baseline_data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert baseline_data.get("unlimited") == []
    assert lint_main() == 0

    def load_services(rel_path: str) -> dict[str, Any]:
        path = ROOT / rel_path
        assert path.exists(), f"missing compose file: {rel_path}"
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data.get("services", {})

    # Postgres (4g)
    for pg_rel in (
        "platform/01.postgres/compose.yaml",
        "finance_report/finance_report/01.postgres/compose.yaml",
        "truealpha/truealpha/01.postgres/compose.yaml",
    ):
        services = load_services(pg_rel)
        assert services.get("postgres", {}).get("mem_limit") == "4g"
        assert services.get("vault-agent", {}).get("mem_limit") == "128m"

    # Redis (1g)
    for redis_rel in (
        "platform/02.redis/compose.yaml",
        "finance_report/finance_report/02.redis/compose.yaml",
    ):
        services = load_services(redis_rel)
        assert services.get("redis", {}).get("mem_limit") == "1g"
        assert services.get("vault-agent", {}).get("mem_limit") == "128m"

    # SigNoz/OTel/Zookeeper (2g)
    signoz_svcs = load_services("platform/11.signoz/compose.yaml")
    assert signoz_svcs.get("signoz", {}).get("mem_limit") == "2g"
    assert signoz_svcs.get("otel-collector", {}).get("mem_limit") == "2g"

    ch_svcs = load_services("platform/03.clickhouse/compose.yaml")
    assert ch_svcs.get("zookeeper", {}).get("mem_limit") == "2g"

    # iac-runner (3g)
    iac_svcs = load_services("bootstrap/06.iac_runner/compose.yaml")
    assert iac_svcs.get("iac-runner", {}).get("mem_limit") == "3g"
    assert iac_svcs.get("vault-agent", {}).get("mem_limit") == "128m"

    # Vault/Alerting/1Password (1g)
    vault_svcs = load_services("bootstrap/05.vault/compose.yaml")
    assert vault_svcs.get("vault", {}).get("mem_limit") == "1g"
    assert vault_svcs.get("unsealer", {}).get("mem_limit") == "1g"

    alert_svcs = load_services("platform/12.alerting/compose.yaml")
    assert alert_svcs.get("feishu-alert-bridge", {}).get("mem_limit") == "1g"
    assert alert_svcs.get("vault-agent", {}).get("mem_limit") == "128m"

    op_svcs = load_services("bootstrap/04.1password/compose.yaml")
    assert op_svcs.get("op-connect-api", {}).get("mem_limit") == "1g"
    assert op_svcs.get("op-connect-sync", {}).get("mem_limit") == "1g"
