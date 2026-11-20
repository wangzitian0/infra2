"""Authentik SSO setup tasks are environment-specific and fail closed (#1807)."""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from invoke.exceptions import Exit

REPO = Path(__file__).resolve().parents[2]
SECRET = "tok-SECRET-0123456789-abcdefghijklmnop"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def st():
    return _load("authentik_shared_under_test", "platform/10.authentik/shared_tasks.py")


def _env(suffix="-staging", name="staging"):
    return {
        "ENV": name,
        "ENV_SUFFIX": suffix,
        "ENV_DOMAIN_SUFFIX": suffix,
        "INTERNAL_DOMAIN": "example.test",
        "VPS_HOST": "203.0.113.1",
    }


def _patch(st, monkeypatch, secrets, vault="vault-admin", remote=None):
    import common.security.store as store

    monkeypatch.setattr(st, "get_env", lambda: _env())
    if callable(secrets):
        monkeypatch.setattr(store, "get_secrets", secrets)
    else:
        monkeypatch.setattr(store, "get_secrets", lambda *a, **k: secrets)
    monkeypatch.setattr(st, "vault_token", lambda: vault)
    calls = []

    def fake_remote(*a, **k):
        calls.append(a)
        return remote if remote is not None else (0, "", "")

    monkeypatch.setattr(st, "_run_remote_script", fake_remote)
    return calls


@pytest.mark.parametrize("key", ["root_token", "api_token"])
def test_existing_token_returns_early_without_ssh(st, monkeypatch, capsys, key):
    calls = _patch(st, monkeypatch, {key: SECRET})
    assert st.create_root_token.body(SimpleNamespace()) is True
    assert calls == []
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err


def test_remote_failure_raises_exit_and_prints_no_token(st, monkeypatch, capsys):
    _patch(st, monkeypatch, {}, vault=SECRET, remote=(12, "", "FAIL: x"))
    with pytest.raises(Exit) as exc:
        st.create_root_token.body(SimpleNamespace())
    assert exc.value.code == 1
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err


def test_success_output_has_no_token_text(st, monkeypatch, capsys):
    answers = iter([{}, {"root_token": SECRET}])
    calls = _patch(st, monkeypatch, lambda *a, **k: next(answers))
    assert st.create_root_token.body(SimpleNamespace()) is True
    assert len(calls) == 1
    out = capsys.readouterr().out
    assert SECRET not in out and SECRET[:20] not in out


def _run_script(st, tmp_path, suffix, wd_label, dirs):
    root = tmp_path / "compose"
    for d in dirs:
        (root / d / "code/platform/10.authentik").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    label = wd_label.replace("ROOT", str(root))
    (bin_dir / "docker").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = inspect ]; then\n'
        f'  [ "$4" = "platform-authentik-server{suffix}" ] || exit 1\n'
        f'  printf %s "{label}"; exit 0\n'
        "fi\n"
        f'echo "$PWD ENV_SUFFIX=$ENV_SUFFIX $*" >> {log}\n'
    )
    (bin_dir / "docker").chmod(0o755)
    script = st.build_remote_script(
        "staging" if suffix else "production", suffix, "example.test", suffix, str(root)
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "VAULT_INIT_TOKEN": "t",
    }
    res = subprocess.run(
        ["bash", "-s"], input=script, capture_output=True, text=True, env=env, timeout=30
    )
    return res, log


@pytest.mark.parametrize(
    "suffix,chosen",
    [("-staging", "platform-authentik-stg"), ("", "platform-authentik-prd")],
)
def test_script_runs_only_in_this_environments_directory(st, tmp_path, suffix, chosen):
    wd = f"ROOT/{chosen}/code/platform/10.authentik"
    res, log = _run_script(
        st, tmp_path, suffix, wd, ["platform-authentik-stg", "platform-authentik-prd"]
    )
    assert res.returncode == 0, res.stderr
    lines = log.read_text().splitlines()
    assert len(lines) == 1
    assert f"/{chosen}/code/platform/10.authentik" in lines[0]
    assert f"ENV_SUFFIX={suffix} " in lines[0]


def test_script_fails_closed_on_zero_matching_directories(st, tmp_path):
    res, log = _run_script(
        st,
        tmp_path,
        "-staging",
        "ROOT/other/code/platform/10.authentik",
        ["platform-authentik-prd", "platform-authentik-stg"],
    )
    assert res.returncode == 12
    assert "expected 1" in res.stderr
    assert not log.exists()


def test_script_fails_closed_when_environment_container_missing(st, tmp_path):
    res, log = _run_script(st, tmp_path, "", "", ["a-platform-authentik-x", "platform-authentik-y"])
    assert res.returncode == 11
    assert "not found" in res.stderr
    assert not log.exists()


def test_script_fails_closed_on_two_matching_directories(st, tmp_path):
    # The label path reaches both checkouts when one is a symlink alias of the other.
    root = tmp_path / "compose"
    real = root / "platform-authentik-a/code/platform/10.authentik"
    real.mkdir(parents=True)
    alias_dir = root / "platform-authentik-b/code/platform"
    alias_dir.mkdir(parents=True)
    (alias_dir / "10.authentik").symlink_to(real)
    script = st.build_remote_script("staging", "-staging", "example.test", "", str(root))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = inspect ]; then printf %s "$FAKE_WD"; exit 0; fi\n'
        f'echo ran >> {tmp_path}/docker.log\n'
    )
    (bin_dir / "docker").chmod(0o755)
    # Realpath comparison makes both directories equal the label: expect fail closed.
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_WD": str(real)}
    res = subprocess.run(
        ["bash", "-s"], input=script, capture_output=True, text=True, env=env, timeout=30
    )
    assert res.returncode == 12
    assert not (tmp_path / "docker.log").exists()


def test_ssh_script_is_noninteractive_and_bounded(monkeypatch):
    from common.deploy import vps_ssh

    seen = {}

    def fake_run(args, **kw):
        seen.update(args=args, **kw)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vps_ssh.subprocess, "run", fake_run)
    vps_ssh.ssh_script("echo hi", "h", "root", "", 22, timeout=7)
    assert "BatchMode=yes" in seen["args"] and "ConnectTimeout=10" in seen["args"]
    assert seen["input"] == "echo hi" and seen["timeout"] == 7
    assert "echo hi" not in seen["args"]


def test_ssh_script_timeout_returns_nonzero(monkeypatch):
    from common.deploy import vps_ssh

    def boom(args, **kw):
        raise subprocess.TimeoutExpired(args, 1)

    monkeypatch.setattr(vps_ssh.subprocess, "run", boom)
    rc, _, err = vps_ssh.ssh_script("x", "h", "root", "", timeout=1)
    assert rc == 124 and "timed out" in err


def test_fail_closed_turns_false_into_exit(st):
    @st.fail_closed
    def bad():
        return False

    with pytest.raises(Exit) as exc:
        bad()
    assert exc.value.code == 1


@pytest.mark.parametrize(
    "rel", ["platform/30.todo/deploy.py", "platform/21.portal/deploy.py"]
)
def test_sso_setup_stops_and_exits_nonzero_on_failed_step(rel, monkeypatch):
    mod = _load("sso_under_test_" + rel.split("/")[1].replace(".", "_"), rel)
    commands = []

    def fake_run(c, cmd, desc, hide=True):
        commands.append(cmd)
        return SimpleNamespace(ok=False)

    monkeypatch.setattr(mod, "run_with_status", fake_run)
    deployer = next(
        v for k, v in vars(mod).items() if k.endswith("Deployer") and k != "Deployer"
    )
    monkeypatch.setattr(deployer, "env", classmethod(lambda cls: _env("", "production")))
    with pytest.raises(Exit) as exc:
        mod.sso_setup.body(SimpleNamespace())
    assert exc.value.code == 1
    assert commands == ["invoke authentik.shared.create-root-token"]
