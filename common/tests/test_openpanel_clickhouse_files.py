"""The op-ch ClickHouse files are inline in the OpenPanel compose, not checkout mounts (#1822).

A raw Dokploy compose has no checkout, so op-ch cannot mount `./clickhouse/*`. The op-ch
`entrypoint` writes the server config, the user config and `init-db.sh`, then runs the
image entrypoint `/entrypoint.sh`. These tests run that text in `sh` with the absolute
paths moved into a temporary directory, and read the files that it writes.
"""

from __future__ import annotations

import hashlib
import stat
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

from common.deploy.compose_compile import load_stack, raw_ineligibility

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = "platform/24.openpanel/compose.yaml"
SERVICE = "op-ch"
CONFIG = "etc/clickhouse-server/config.d/op-config.xml"
USERS = "etc/clickhouse-server/users.d/op-user-config.xml"
INIT = "docker-entrypoint-initdb.d/init-db.sh"
#: The data mount. A change moves or resets the ClickHouse data.
DATA_MOUNT = "${DATA_PATH}/op-ch:/var/lib/clickhouse"

#: sha256 of each old checkout file, the files that op-ch mounted before #1822. The
#: values come from `git show e88287bd:platform/24.openpanel/clickhouse/<file> |
#: shasum -a 256` (main on 2026-11-26, the last commit with the files). The wrapper
#: must write the same bytes; a changed file needs a deliberate new value here.
OLD_FILE_SHA256 = {
    CONFIG: "5bbc59b6c3cf79f3a6af19a6563bf1f7229c608c3c6aae9a001b5fe3f513c5fc",  # clickhouse-config.xml
    USERS: "87d34f9b5e58b1ec4067f6339797b0626c0c66d9bdcdc4a07c6b078ad0bc94d3",  # clickhouse-user-config.xml
    INIT: "ce8861a900a663a9f497a5dceaba4990932f1c77b258dcce64b5580a2fa775a3",  # init-db.sh
}


def _spec() -> dict:
    """The op-ch service of the compiled text (the text that the deploy uploads)."""
    return yaml.safe_load(load_stack(COMPOSE).compiled)["services"][SERVICE]


def _script() -> str:
    entrypoint = _spec()["entrypoint"]
    assert entrypoint[:2] == ["sh", "-c"] and len(entrypoint) == 3
    return entrypoint[2]


def _run(tmp_path: Path) -> Path:
    """Run the op-ch wrapper under `tmp_path`; return the root of the written files."""
    root = tmp_path / "root"
    args = tmp_path / "entrypoint-args"
    fake = tmp_path / "entrypoint.sh"
    fake.write_text(f'#!/bin/sh\necho "$#" > {args}\n', encoding="utf-8")
    fake.chmod(0o755)
    script = _script()
    assert script.count("exec /entrypoint.sh\n") == 1
    moved = (
        script.replace(" /etc/clickhouse-server/", f" {root}/etc/clickhouse-server/")
        .replace(" /docker-entrypoint-initdb.d", f" {root}/docker-entrypoint-initdb.d")
        .replace("exec /entrypoint.sh\n", f"exec {fake}\n")
    )
    assert "/etc/" not in moved.replace(f"{root}/etc/", "")
    proc = subprocess.run(
        ["sh", "-c", moved], capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, proc.stderr
    # The image entrypoint runs the server when it gets no argument.
    assert args.read_text(encoding="utf-8") == "0\n"
    return root


def test_op_ch_mounts_nothing_from_the_checkout() -> None:
    assert _spec()["volumes"] == [DATA_MOUNT]
    assert [
        r
        for r in raw_ineligibility(load_stack(COMPOSE).compiled)
        if r.startswith(f"services.{SERVICE} ")
    ] == []
    assert not (ROOT / "platform/24.openpanel/clickhouse").exists()


def test_compose_interpolation_does_not_change_the_script() -> None:
    # Compose interpolates `$`. The script has none, so the container gets the file text.
    assert "$" not in _script()


def test_init_db_keeps_its_run_once_semantics() -> None:
    # The image entrypoint runs /docker-entrypoint-initdb.d only when the data dir has
    # no `data` sub-directory. This flag makes it run on each start.
    env = _spec().get("environment") or {}
    assert "CLICKHOUSE_ALWAYS_RUN_INITDB_SCRIPTS" not in env
    assert env.get("CLICKHOUSE_SKIP_USER_SETUP") == "1"
    assert "command" not in _spec()


def test_the_wrapper_writes_the_server_config(tmp_path: Path) -> None:
    cfg = ET.fromstring((_run(tmp_path) / CONFIG).read_text(encoding="utf-8"))
    assert cfg.tag == "clickhouse"
    # The cgroup mem_limit (2560m) must stay above the server cap (2 GiB).
    assert int(cfg.findtext("max_server_memory_usage")) == 2 * 1024**3
    assert _spec()["mem_limit"] == "2560m"
    assert int(cfg.findtext("background_schedule_pool_size")) <= 64
    assert cfg.find("background_pool_size") is None
    for log in ("query_log", "query_thread_log", "text_log", "trace_log", "part_log"):
        assert cfg.find(log).get("remove") == "remove", log


def test_the_wrapper_writes_the_user_config(tmp_path: Path) -> None:
    usr = ET.fromstring((_run(tmp_path) / USERS).read_text(encoding="utf-8"))
    profile = usr.find("profiles/default")
    assert {child.tag: child.text for child in profile} == {
        "log_queries": "0",
        "log_query_threads": "0",
        "max_threads": "2",
        "async_insert": "1",
        "wait_for_async_insert": "1",
    }


def test_the_wrapper_writes_an_executable_init_script(tmp_path: Path) -> None:
    init = _run(tmp_path) / INIT
    # An executable *.sh runs as its own process; else the entrypoint sources it.
    assert stat.S_IMODE(init.stat().st_mode) == 0o755
    text = init.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/bash\nset -e\n")
    assert "CREATE DATABASE IF NOT EXISTS openpanel;" in text
    syntax = subprocess.run(
        ["bash", "-n", str(init)], capture_output=True, text=True, timeout=30
    )
    assert syntax.returncode == 0, syntax.stderr


@pytest.mark.parametrize("name", [CONFIG, USERS, INIT])
def test_a_restart_writes_the_same_file(tmp_path: Path, name: str) -> None:
    # `restart: unless-stopped` runs the wrapper again on the files of the first start.
    first = (_run(tmp_path) / name).read_bytes()
    assert (_run(tmp_path) / name).read_bytes() == first


@pytest.mark.parametrize("name", [CONFIG, USERS, INIT])
def test_the_wrapper_writes_the_bytes_of_the_old_file(
    tmp_path: Path, name: str
) -> None:
    """Each written file has the bytes of the old checkout file.

    The expected values are `OLD_FILE_SHA256`: `git show
    e88287bd:platform/24.openpanel/clickhouse/<file> | shasum -a 256` on main.
    """
    written = (_run(tmp_path) / name).read_bytes()
    assert hashlib.sha256(written).hexdigest() == OLD_FILE_SHA256[name]
