"""Tests for tools.ci_runner unified CI gate runner (#1000)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from tools.ci_runner import (
    DUMMY_COMPOSE_ENV,
    GATES,
    _default_pytest_workers,
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


def test_ci_runner_deployers_gate_succeeds() -> None:
    ret = run_deployers(verbose=False)
    assert ret == 0


def test_ci_runner_harness_gate_succeeds() -> None:
    ret = run_harness(verbose=False)
    assert ret == 0


def test_ci_runner_vault_agents_only_succeeds() -> None:
    ret = run_vault(agents_only=True, verbose=False)
    assert ret == 0


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

    monkeypatch.setattr("tools.ci_runner.ROOT", test_root)
    ret = run_vault(agents_only=True, verbose=False)
    assert ret == 1
    captured = capsys.readouterr()
    assert "Missing 'exit_on_err = true'" in captured.out


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

    monkeypatch.setattr("tools.ci_runner.ROOT", test_root)
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

    monkeypatch.setattr("tools.ci_runner._run_cmd", mock_run_cmd)
    ret = run_unit_tests(verbose=False, timeout=0.01)
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


def test_default_pytest_workers_caps_local_and_uses_auto_in_ci(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CI", "true")
    assert _default_pytest_workers() == "auto"

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr("os.cpu_count", lambda: 14)
    assert _default_pytest_workers() == "4"

    monkeypatch.setattr("os.cpu_count", lambda: 2)
    assert _default_pytest_workers() == "2"


def test_run_unit_tests_changed_only_executes_fail_fast_subset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    recorded_cmds: list[list[str]] = []

    def mock_run_cmd(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded_cmds.append(list(cmd))
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr("tools.ci_runner._run_cmd", mock_run_cmd)
    monkeypatch.setattr(
        "tools.ci_runner._resolve_changed_test_targets",
        lambda: ["common/tests/test_ci_runner.py"],
    )
    ret = main(["unit-tests", "--changed"])
    assert ret == 0
    pytest_cmds = [cmd for cmd in recorded_cmds if "pytest" in cmd]
    assert len(pytest_cmds) == 1
    assert "common/tests/test_ci_runner.py" in pytest_cmds[0]
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
    assert _select_changed_compose_targets(all_compose, ["docs/README.md"]) == []


def test_test_affected_delegates_to_ci_runner_and_caps_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess
    from tools import test_affected

    assert test_affected.resolve_affected_tests(["tools/ci_runner.py"]) == [
        "common/tests/test_ci_runner.py"
    ]

    recorded: list[list[str]] = []

    def fake_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded.append(list(cmd))
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(
        test_affected,
        "get_changed_files",
        lambda base_ref="origin/main": [
            "common/coverage_regression.py",
            "tools/ci_runner.py",
        ],
    )
    monkeypatch.setattr(test_affected, "_default_pytest_workers", lambda: "4")
    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = test_affected.main([])
    assert rc == 0
    assert len(recorded) == 1
    cmd = recorded[0]
    assert "-x" in cmd
    assert "-n" in cmd and cmd[cmd.index("-n") + 1] == "4"
    assert "common/tests/test_ci_runner.py" in cmd
    assert "common/tests/test_coverage_regression.py" in cmd
    assert "common/tests/test_coverage_regression_audit_cli.py" in cmd
