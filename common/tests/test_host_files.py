"""Repository files that deploy.py writes to a host path for a bind mount (#1822)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from common.deploy import deploy_guard_audit
from common.deploy.config_hash import (
    compose_mount_fingerprint_gaps,
    mount_fingerprint_env,
)
from common.deploy.compose_compile import raw_ineligibility
from common.deploy.host_files import deliver_host_files, host_file_command
from common.tests.conftest import load_repo_module

ROOT = Path(__file__).resolve().parents[2]

# service directory -> (deploy.py class, compose service, {host file name: mode})
DELIVERED = {
    "platform/10.authentik": (
        "AuthentikDeployer",
        "token-init",
        {"init-token.sh": "0755"},
    ),
    "platform/24.openpanel": (
        "OpenPanelDeployer",
        "op-ch",
        {
            "clickhouse-config.xml": "0644",
            "clickhouse-user-config.xml": "0644",
            "clickhouse-init-db.sh": "0755",
        },
    ),
}


class _Result:
    def __init__(self, ok: bool) -> None:
        self.ok = ok


def _run_local(command: str) -> _Result:
    return _Result(subprocess.run(["sh", "-c", command], timeout=30).returncode == 0)


# --- host_file_command ------------------------------------------------------------------


def test_the_command_writes_the_exact_bytes_and_mode(tmp_path: Path) -> None:
    content = b"#!/bin/sh\necho '$HOME' \"$(id)\" `x` ; rm -rf /\n\x00\xff"
    target = tmp_path / "sub" / "f.sh"
    assert _run_local(host_file_command(str(target), content, "0755")).ok
    assert target.read_bytes() == content
    assert oct(target.stat().st_mode & 0o777) == "0o755"
    assert not (tmp_path / "sub" / "f.sh.tmp").exists()


def test_the_command_replaces_an_old_file(tmp_path: Path) -> None:
    target = tmp_path / "f.xml"
    target.write_bytes(b"old")
    assert _run_local(host_file_command(str(target), b"new", "0644")).ok
    assert target.read_bytes() == b"new"
    assert oct(target.stat().st_mode & 0o777) == "0o644"


@pytest.mark.parametrize("path", ["relative/f", "/dir/", ""])
def test_a_path_that_is_not_an_absolute_file_raises(path: str) -> None:
    with pytest.raises(ValueError, match="absolute file path"):
        host_file_command(path, b"x")


@pytest.mark.parametrize("mode", ["755", "0855", "0644; rm -rf /", "u+x"])
def test_a_mode_that_is_not_four_octal_digits_raises(mode: str) -> None:
    with pytest.raises(ValueError, match="4 octal digits"):
        host_file_command("/x/f", b"x", mode)


# --- deliver_host_files -----------------------------------------------------------------


def test_no_host_delivers_nothing_and_fails() -> None:
    assert deliver_host_files(None, {"/x/f": (b"x", "0644")}) is False


def test_the_first_failure_stops_the_delivery() -> None:
    calls: list[str] = []

    def on_host(command: str) -> _Result:
        calls.append(command)
        return _Result(False)

    files = {"/x/a": (b"a", "0644"), "/x/b": (b"b", "0644")}
    assert deliver_host_files(on_host, files) is False
    assert len(calls) == 1 and "/x/a" in calls[0]


def test_every_file_is_written_on_success() -> None:
    calls: list[str] = []
    files = {"/x/a": (b"a", "0644"), "/x/b": (b"b", "0755")}
    assert deliver_host_files(lambda cmd: calls.append(cmd) or _Result(True), files)
    assert calls == [host_file_command(p, c, m) for p, (c, m) in files.items()]


# --- the stacks that deliver files ------------------------------------------------------


@pytest.mark.parametrize("service_dir", sorted(DELIVERED))
def test_the_compose_mounts_the_host_path_with_a_fingerprint(service_dir: str) -> None:
    _cls, service, files = DELIVERED[service_dir]
    text = (ROOT / service_dir / "compose.yaml").read_text(encoding="utf-8")
    spec = yaml.safe_load(text)["services"][service]
    for name in files:
        assert any(v.startswith(f"${{DATA_PATH}}/{name}:") for v in spec["volumes"])
        assert (service, name) not in compose_mount_fingerprint_gaps(text)
        assert (ROOT / service_dir / name).is_file()
    assert not [
        r for r in raw_ineligibility(text) if r.startswith(f"services.{service} ")
    ]
    composes = [ROOT / service_dir / "compose.yaml"]
    assert deploy_guard_audit.host_mount_violations(composes) == []


@pytest.mark.parametrize("service_dir", sorted(DELIVERED))
def test_composing_writes_the_repository_bytes_then_deploys(
    monkeypatch, service_dir: str
) -> None:
    from common.deploy.deployer import Deployer

    cls_name, _service, files = DELIVERED[service_dir]
    cls = getattr(
        load_repo_module(f"{service_dir}/deploy.py", f"host_files_{cls_name}"), cls_name
    )
    monkeypatch.setattr(
        cls, "env", classmethod(lambda _c: {"ENV": "staging", "ENV_SUFFIX": "-staging"})
    )
    commands: list[str] = []
    monkeypatch.setattr(
        cls,
        "_on_host",
        staticmethod(lambda _c, _e: lambda cmd: commands.append(cmd) or _Result(True)),
    )
    seen: dict = {}
    monkeypatch.setattr(
        Deployer,
        "composing",
        classmethod(lambda _c, _ctx, env_vars: seen.update(env_vars) or "id"),
    )
    assert cls.composing(None, {}) == "id"
    data_path = cls.data_path_for_env(cls.env())
    expected = {
        f"{data_path}/{name}": ((ROOT / service_dir / name).read_bytes(), mode)
        for name, mode in files.items()
    }
    assert commands == [host_file_command(p, c, m) for p, (c, m) in expected.items()]
    rendered = {name: (ROOT / service_dir / name).read_bytes() for name in files}
    assert {
        k: seen[k] for k in mount_fingerprint_env(rendered)
    } == mount_fingerprint_env(rendered)


@pytest.mark.parametrize("service_dir", sorted(DELIVERED))
def test_composing_stops_before_the_deploy_when_a_write_fails(
    monkeypatch, service_dir: str
) -> None:
    from common.deploy.deployer import Deployer

    cls_name, _service, files = DELIVERED[service_dir]
    cls = getattr(
        load_repo_module(f"{service_dir}/deploy.py", f"host_files_f_{cls_name}"),
        cls_name,
    )
    monkeypatch.setattr(
        cls, "env", classmethod(lambda _c: {"ENV": "staging", "ENV_SUFFIX": "-staging"})
    )
    monkeypatch.setattr(
        cls, "_on_host", staticmethod(lambda _c, _e: lambda _cmd: _Result(False))
    )
    deployed: list[str] = []
    monkeypatch.setattr(
        Deployer, "composing", classmethod(lambda *_a: deployed.append("x") or "id")
    )
    with pytest.raises(RuntimeError, match="Failed to deliver"):
        cls.composing(None, {})
    assert deployed == []
