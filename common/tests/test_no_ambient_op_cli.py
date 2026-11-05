"""A unit test never runs the 1Password CLI of the machine (#1453)."""

from __future__ import annotations

import stat
import subprocess

import pytest
from infra2_sdk.secrets import OnePasswordBackend


def _fake_op_on_path(tmp_path, monkeypatch):
    """Put an ``op`` on PATH that leaves a marker file when it runs."""
    marker = tmp_path / "op-ran"
    script = tmp_path / "op"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\necho '{{}}'\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    return marker


def test_the_default_runner_cannot_reach_an_op_binary_on_path(tmp_path, monkeypatch):
    marker = _fake_op_on_path(tmp_path, monkeypatch)

    with pytest.raises(FileNotFoundError):
        OnePasswordBackend("Infra2").read("init/env_vars")

    assert not marker.exists(), "the real op binary ran inside a unit test"


def test_a_backend_with_its_own_runner_is_unchanged():
    calls: list[list[str]] = []

    def runner(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout='{"fields": []}', stderr="")

    assert OnePasswordBackend("Infra2", runner=runner).read("init/env_vars") == {}
    assert calls and calls[0][:3] == ["op", "item", "get"]
