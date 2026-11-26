"""Contract: every ClickHouse service has a memory cap (infra-stability-hardening).

The shared host runs swapless (by choice). An uncapped ClickHouse defaults
max_server_memory_usage to ~90% of TOTAL host RAM, so a single heavy merge/query can
trip the kernel OOM killer and take down an unrelated container (Dokploy, prod). This
contract stops a future "rightsize" from again tuning only CPU and leaving memory
unbounded: both CH instances must carry a compose `mem_limit` (cgroup backstop), and
op-ch must also carry an absolute server-side cap.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]

# compose file -> ClickHouse service name within it
CLICKHOUSE_COMPOSES = {
    "platform/03.clickhouse/compose.yaml": "clickhouse",
    "platform/24.openpanel/compose.yaml": "op-ch",
}


def test_every_clickhouse_service_has_a_compose_mem_limit():
    for rel, service in CLICKHOUSE_COMPOSES.items():
        compose = yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))
        svc = compose["services"][service]
        assert svc.get("mem_limit"), (
            f"{rel} service {service!r} must set a compose `mem_limit` (cgroup cap): "
            "the shared swapless host OOM-kills an uncapped ClickHouse's neighbors."
        )


# The op-ch absolute server cap is in
# test_openpanel_clickhouse_files.py::test_the_wrapper_writes_the_server_config.
