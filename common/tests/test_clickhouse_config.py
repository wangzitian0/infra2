"""Tests for ClickHouse and Authentik resource bounds (#1190).

Ensures ClickHouse background schedule pools, memory caps, cache sizes,
async insert batching, and Authentik container memory limits adhere to
the resource stabilization specification.
"""

from __future__ import annotations

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

    assert server.get("mem_limit") == "512m"
    assert worker.get("mem_limit") == "384m"

    assert server.get("healthcheck", {}).get("interval") == "60s"
    assert worker.get("healthcheck", {}).get("interval") == "60s"
