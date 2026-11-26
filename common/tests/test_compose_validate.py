"""`docker compose config` validates the compiled text of an x-infra2 stack (infra2#1884)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from common.deploy import compose_validate
from common.deploy.compose_compile import has_x_infra2, load_stack
from common.deploy.compose_validate import (
    CI_COMPOSE_ENV,
    config_command,
    validate,
)

ROOT = Path(__file__).resolve().parents[2]
TODO = "platform/30.todo/compose.yaml"
INFRA_CI = ROOT / ".github/workflows/infra-ci.yml"


def _tracked_compose_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "*compose.yaml"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.split()
    return sorted(p for p in out if Path(p).name == "compose.yaml")


def test_every_plain_file_keeps_the_source_command(tmp_path):
    plain = [
        p
        for p in _tracked_compose_files()
        if not has_x_infra2((ROOT / p).read_text(encoding="utf-8"))
    ]
    # 15 plain files on 2026-11-26, after platform/02.redis moved to x-infra2 (#1822).
    assert len(plain) >= 15
    for rel in plain:
        assert config_command(rel, tmp_path) == [
            "docker",
            "compose",
            "-f",
            rel,
            "config",
        ], rel
    assert list(tmp_path.iterdir()) == []


def test_the_x_infra2_stack_validates_its_compiled_text(tmp_path):
    assert has_x_infra2((ROOT / TODO).read_text(encoding="utf-8"))
    cmd = config_command(TODO, tmp_path)
    assert cmd[:3] == ["docker", "compose", "-f"] and cmd[4:] == ["config"]
    written = Path(cmd[3])
    # The work copy keeps the repository-relative path, so the project name stays `30.todo`.
    assert written == tmp_path / TODO
    text = written.read_text(encoding="utf-8")
    assert text == load_stack(TODO).compiled
    assert text != (ROOT / TODO).read_text(encoding="utf-8")
    assert "x-infra2" not in yaml.safe_load(text)
    assert "vault-agent" in yaml.safe_load(text)["services"]


def test_validate_reports_failed_files_and_uses_the_ci_env():
    seen: list[tuple[list[str], dict[str, str]]] = []

    # The failing sample is a plain file, so docker compose reads its source path.
    # platform/02.redis moved to x-infra2 (#1822); it validates a compiled work copy.
    bad = "platform/03.clickhouse/compose.yaml"

    def fake_run(cmd, *, env, stdout):
        assert stdout is subprocess.DEVNULL
        seen.append((list(cmd), env))
        rc = 1 if cmd[cmd.index("-f") + 1] == bad else 0
        return subprocess.CompletedProcess(cmd, rc)

    failed = validate(
        [bad, TODO, "platform/01.postgres/compose.yaml"],
        run=fake_run,
    )
    assert failed == [bad]
    assert len(seen) == 3
    for _cmd, env in seen:
        assert {k: env[k] for k in CI_COMPOSE_ENV} == CI_COMPOSE_ENV
    assert seen[1][0][3] != TODO and seen[1][0][3].endswith(TODO)


def test_validate_fails_an_x_infra2_file_that_does_not_compile(tmp_path, capsys):
    bad = tmp_path / "compose.yaml"
    bad.write_text(
        "x-infra2: {}\nservices:\n  a:\n    image: x\n    x-capabilities: [nope]\n",
        encoding="utf-8",
    )

    def never_run(cmd, **_kw):
        raise AssertionError(f"docker must not run for a compile failure: {cmd}")

    assert validate([bad], run=never_run) == [str(bad)]
    assert "x-infra2 compile failed" in capsys.readouterr().err


def test_tool_validate_exits_1_on_a_failed_file(monkeypatch, capsys):
    from tools import compose_compile as tool

    monkeypatch.setattr(
        compose_validate.subprocess,
        "run",
        lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 1),
    )
    assert tool.main(["validate", TODO]) == 1
    assert f"invalid compose configuration: {TODO}" in capsys.readouterr().err
    monkeypatch.setattr(
        compose_validate.subprocess,
        "run",
        lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 0),
    )
    assert tool.main(["validate", TODO]) == 0


def _validate_step() -> dict:
    jobs = yaml.safe_load(INFRA_CI.read_text(encoding="utf-8"))["jobs"]
    steps = jobs["validate-compose"]["steps"]
    matches = [s for s in steps if s.get("name") == "Validate all compose.yaml files"]
    assert len(matches) == 1
    names = [s.get("name") for s in steps]
    assert names.index("Install lint dependencies") < names.index(
        "Validate all compose.yaml files"
    )
    return matches[0]


def test_the_ci_job_runs_the_compiled_validation():
    step = _validate_step()
    assert "python3 -m tools.compose_compile validate" in step["run"]
    assert "docker compose -f" not in step["run"]
    # CI_COMPOSE_ENV is the one dummy environment; the step must not define a second one.
    assert "env" not in step


@pytest.mark.parametrize("key", ["ENV", "ENV_SUFFIX", "VAULT_ADDR", "IMAGE_DIGEST"])
def test_ci_compose_env_holds_the_deploy_variables(key):
    assert CI_COMPOSE_ENV[key]
