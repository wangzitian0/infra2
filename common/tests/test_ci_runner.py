"""Tests for common.gate.ci_runner unified CI gate runner (#1000)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from common.gate.ci_runner import (
    DUMMY_COMPOSE_ENV,
    GATES,
    _resolve_changed_test_targets,
    _select_changed_compose_targets,
    main,
    run_compose,
    run_deployers,
    run_harness,
    run_preflight,
    run_unit_tests,
    run_vault,
)

ROOT = Path(__file__).resolve().parents[2]


def test_ci_runner_registered_gates() -> None:
    expected_gates = {
        "preflight",
        "compose",
        "deployers",
        "vault",
        "harness",
        "lint",
        "unit-tests",
    }
    assert expected_gates.issubset(GATES.keys())
    for name in expected_gates:
        desc, fn = GATES[name]
        assert desc and len(desc) > 5
        assert callable(fn)


def test_ci_runner_dummy_compose_env_complete() -> None:
    required_keys = {
        "DATA_PATH",
        "ENV",
        "INTERNAL_DOMAIN",
        "VAULT_ADDR",
        "FEISHU_APP_ID",
        "LLM_API_KEY",
    }
    assert required_keys.issubset(DUMMY_COMPOSE_ENV.keys())


def test_ci_runner_uses_the_one_compose_env_and_command() -> None:
    """infra2#1884: ci_runner and the CI job share one env and one command builder."""
    import common.gate.ci_runner as runner
    from common.deploy import compose_validate

    assert runner.DUMMY_COMPOSE_ENV is compose_validate.CI_COMPOSE_ENV
    assert runner.config_command is compose_validate.config_command


def test_ci_runner_cli_list(capsys: pytest.CaptureFixture[str]) -> None:
    ret = main(["--list"])
    assert ret == 0
    captured = capsys.readouterr()
    assert "Available CI gate targets:" in captured.out
    for name in GATES.keys():
        assert name in captured.out


def test_ci_runner_cli_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "Unified SSOT runner for CI gate checks" in captured.out
    assert "--changed" in captured.out


def test_ci_runner_cli_unknown_target() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["nonexistent-target"])
    assert exc_info.value.code != 0


def test_ci_runner_preflight_gate_succeeds() -> None:
    ret = run_preflight(verbose=False)
    assert ret == 0


def test_ci_runner_compose_gate_succeeds() -> None:
    ret = run_compose(verbose=False)
    assert ret == 0


def test_ci_runner_compose_gate_validates_compiled_x_infra2_text(monkeypatch) -> None:
    """infra2#1884: the local mirror runs `docker compose config` on compiled x-infra2 text.

    A plain file keeps the old command `docker compose -f <rel> config`.
    """
    import subprocess

    import common.gate.ci_runner as runner
    from common.deploy.compose_compile import has_x_infra2, load_stack

    calls: list[tuple[list[str], str | None]] = []

    def fake_run_cmd(cmd, **_kw):
        cmd = list(cmd)
        text = None
        if cmd[:2] == ["docker", "compose"]:
            text = Path(cmd[cmd.index("-f") + 1]).read_text(encoding="utf-8")
        calls.append((cmd, text))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(runner.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    monkeypatch.setattr(runner, "_run_cmd", fake_run_cmd)
    assert run_compose(verbose=False) == 0

    compose_calls = [(c, t) for c, t in calls if c[:2] == ["docker", "compose"]]
    todo = "platform/30.todo/compose.yaml"
    by_rel: dict[str, tuple[list[str], str | None]] = {}
    for cmd, text in compose_calls:
        rel = cmd[cmd.index("-f") + 1]
        if Path(rel).is_absolute():
            rel = rel[rel.index("compose-validate-") :].split("/", 1)[1]
        by_rel[rel] = (cmd, text)
    assert todo in by_rel
    cmd, text = by_rel[todo]
    assert text == load_stack(todo).compiled
    assert text != (ROOT / todo).read_text(encoding="utf-8")
    plain = [
        rel
        for rel in by_rel
        if not has_x_infra2((ROOT / rel).read_text(encoding="utf-8"))
    ]
    # 14 plain files on 2026-11-26 (measured), after platform/03.s3 moved to
    # x-infra2 (#1822). The gate also validates the preview and template files.
    assert len(plain) >= 14
    for rel in plain:
        assert by_rel[rel][0] == ["docker", "compose", "-f", rel, "config"], rel


def test_ci_runner_deployers_gate_succeeds() -> None:
    ret = run_deployers(verbose=False)
    assert ret == 0


def test_ci_runner_harness_gate_succeeds() -> None:
    ret = run_harness(verbose=False)
    assert ret == 0


def test_ci_runner_vault_agents_only_succeeds() -> None:
    ret = run_vault(agents_only=True, verbose=False)
    assert ret == 0


@pytest.mark.parametrize(
    "stack",
    [
        # Names `vault-agent` only in depends_on.
        "finance_report/finance_report/10.app/compose.yaml",
        # Does not name `vault-agent` at all.
        "finance_report/finance_report/02.redis/compose.yaml",
    ],
)
def test_ci_runner_checks_the_compiled_sidecar_of_each_x_infra2_vault_stack(
    monkeypatch, capsys, stack
) -> None:
    """#1822: an x-infra2 stack with `x-infra2.vault` is checked on its compiled text."""
    import common.deploy.compose_compile as compose_compile

    real = compose_compile.load_stack

    def load_stack(path, **kwargs):
        loaded = real(path, **kwargs)
        if str(path) != stack:
            return loaded
        return compose_compile.StackCompose(
            source=loaded.source,
            compiled=loaded.compiled.replace("lookup-self", "lookup"),
            x_infra2=True,
        )

    monkeypatch.setattr(compose_compile, "load_stack", load_stack)
    assert run_vault(agents_only=True, verbose=False) == 1
    assert f"missing token lookup in {stack}" in capsys.readouterr().out


def test_ci_runner_vault_policy_only_succeeds() -> None:
    ret = run_vault(policy_only=True, verbose=False)
    assert ret == 0


def test_ci_runner_vault_mutation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Falsifiability test: a mutated vault-agent.hcl missing exit_on_err fails
    test_root = tmp_path / "mock_repo"
    test_root.mkdir()
    bad_hcl = test_root / "vault-agent.hcl"
    bad_hcl.write_text("exit_on_retry_failure = true\n", encoding="utf-8")

    monkeypatch.setattr("common.gate.ci_runner.ROOT", test_root)
    ret = run_vault(agents_only=True, verbose=False)
    assert ret == 1
    captured = capsys.readouterr()
    assert "Missing 'exit_on_err = true'" in captured.out


def test_ci_runner_vault_require_hcl2_fails_when_missing_or_empty_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import builtins

    test_root = tmp_path / "mock_policy_repo"
    test_root.mkdir()
    pol = test_root / "vault-policy.hcl"
    pol.write_text(
        'path "secret/data/ok" {\n  capabilities = ["read"]\n}\n', encoding="utf-8"
    )
    monkeypatch.setattr("common.gate.ci_runner.ROOT", test_root)

    real_import = builtins.__import__

    def no_hcl2(name, *args, **kwargs):
        if name == "hcl2":
            raise ImportError("no hcl2")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_hcl2)
    assert run_vault(policy_only=True, require_hcl2=False) == 0
    assert run_vault(policy_only=True, require_hcl2=True) == 1
    assert "python-hcl2 is required (--require-hcl2)" in capsys.readouterr().out

    monkeypatch.undo()
    monkeypatch.setattr("common.gate.ci_runner.ROOT", test_root)
    pol.write_text("   \n", encoding="utf-8")
    assert run_vault(policy_only=True) == 1
    assert "Empty policy file" in capsys.readouterr().out


def test_ci_runner_deployer_mutation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Falsifiability test: a deploy.py defining Deployer subclass without service fails
    test_root = tmp_path / "mock_deployer_repo"
    test_root.mkdir()
    bad_deploy = test_root / "deploy.py"
    bad_deploy.write_text(
        "class Deployer:\n"
        "    pass\n"
        "class MockDeployer(Deployer):\n"
        "    service = ''\n"
        "    compose_path = 'compose.yaml'\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("common.gate.ci_runner.ROOT", test_root)
    ret = run_deployers(verbose=False)
    assert ret == 1
    captured = capsys.readouterr()
    assert "missing service name" in captured.out


def test_ci_runner_unit_tests_timeout_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Falsifiability test: run_unit_tests handles TimeoutExpired gracefully
    import subprocess

    def mock_run_cmd(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "pytest" in args[0]:
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=0.01)
        return subprocess.CompletedProcess(args=args[0], returncode=0)

    monkeypatch.setattr("common.gate.ci_runner._run_cmd", mock_run_cmd)
    ret = run_unit_tests(
        targets=["common/tests/test_ci_runner.py"], verbose=False, timeout=0.01
    )
    assert ret == 1
    captured = capsys.readouterr()
    assert "timed out after 0.01s" in captured.out


def test_resolve_changed_test_targets_maps_source_and_service_paths() -> None:
    targets = _resolve_changed_test_targets(
        [
            "tools/ci_runner.py",
            "common/coverage_regression.py",
            "truealpha/truealpha/20.data_engine/compose.yaml",
            "docs/project/README.md",
        ]
    )
    assert "common/tests/test_ci_runner.py" in targets
    assert "common/tests/test_coverage_regression.py" in targets
    assert "common/tests/test_coverage_regression_audit_cli.py" in targets
    assert "common/tests/test_truealpha_data_engine.py" in targets
    assert "common/tests/test_deployer_contract_light.py" in targets
    assert _resolve_changed_test_targets(["docs/project/README.md"]) == []


@pytest.mark.parametrize("argv", [["unit-tests"], ["unit-tests", "--changed"]])
def test_run_unit_tests_runs_the_diff_targets_in_one_process(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    recorded_cmds: list[list[str]] = []

    def mock_run_cmd(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded_cmds.append(list(cmd))
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr("common.gate.ci_runner._run_cmd", mock_run_cmd)
    monkeypatch.setattr(
        "common.gate.ci_runner._resolve_changed_test_targets",
        lambda: ["common/tests/test_ci_runner.py", "common/tests/test_harness.py"],
    )
    ret = main(argv)
    assert ret == 0
    pytest_cmds = [cmd for cmd in recorded_cmds if "pytest" in cmd]
    assert len(pytest_cmds) == 1
    assert "common/tests/test_ci_runner.py" in pytest_cmds[0]
    assert "common/tests/test_harness.py" in pytest_cmds[0]
    assert "common/tests" not in pytest_cmds[0]
    assert "-x" in pytest_cmds[0]
    assert "-n" not in pytest_cmds[0]


def test_select_changed_compose_targets_scopes_services_and_contracts() -> None:
    redis_compose = ROOT / "platform/02.redis/compose.yaml"
    ta_compose = ROOT / "truealpha/truealpha/20.data_engine/compose.yaml"
    fr_compose = ROOT / "finance_report/finance_report/10.app/compose.yaml"
    all_compose = [redis_compose, ta_compose, fr_compose]

    assert _select_changed_compose_targets(
        all_compose, ["platform/02.redis/compose.yaml"]
    ) == [redis_compose]
    assert _select_changed_compose_targets(
        all_compose, ["contracts/truealpha/data-engine.required-env.generated.json"]
    ) == [ta_compose]
    assert _select_changed_compose_targets(
        all_compose, ["contracts/finance_report/required-env.generated.json"]
    ) == [fr_compose]
    assert (
        _select_changed_compose_targets(all_compose, ["tools/ci_runner.py"])
        == all_compose
    )
    # The runner logic lives in common/gate/ci_runner.py (infra2#1823 Step 4.3).
    assert (
        _select_changed_compose_targets(all_compose, ["common/gate/ci_runner.py"])
        == all_compose
    )
    assert _select_changed_compose_targets(all_compose, ["docs/README.md"]) == []


def test_legacy_test_affected_wrapper_is_retired() -> None:
    """Enforce one diff-scoped test runner (common.gate.ci_runner) with no duplicate CLI wrapper."""
    assert not (ROOT / "tools/test_affected.py").exists()
    for doc_rel in (
        "docs/ssot/core.engineering.md",
        "harness/README.md",
        "tools/README.md",
    ):
        text = (ROOT / doc_rel).read_text(encoding="utf-8")
        assert "test_affected.py" not in text, (
            f"{doc_rel} still references retired tools/test_affected.py"
        )
