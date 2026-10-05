"""Tests for tools.ci_runner unified CI gate runner (#1000)."""

from __future__ import annotations

from pathlib import Path

import pytest
from tools.ci_runner import (
    DUMMY_COMPOSE_ENV,
    GATES,
    main,
    run_harness,
    run_preflight,
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


def test_ci_runner_cli_unknown_target() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["nonexistent-target"])
    assert exc_info.value.code != 0


def test_ci_runner_preflight_gate_succeeds() -> None:
    ret = run_preflight(verbose=False)
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


def test_ci_runner_vault_mutation_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Falsifiability test: a mutated vault-agent.hcl missing exit_on_err fails
    test_root = tmp_path / "mock_repo"
    test_root.mkdir()
    bad_hcl = test_root / "vault-agent.hcl"
    bad_hcl.write_text("exit_on_retry_failure = true\n", encoding="utf-8")

    monkeypatch.setattr("tools.ci_runner.ROOT", test_root)
    ret = run_vault(agents_only=True, verbose=False)
    assert ret == 1


def test_ci_runner_deployer_mutation_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Falsifiability test: a deploy.py defining Deployer without service fails
    from tools.ci_runner import run_deployers

    test_root = tmp_path / "mock_deployer_repo"
    test_root.mkdir()
    bad_deploy = test_root / "deploy.py"
    bad_deploy.write_text(
        "class MockDeployer:\n"
        "    service = ''\n"
        "    compose_path = 'compose.yaml'\n"
        "MockDeployer.__mro__ = (MockDeployer, type('Deployer', (), {}), object)\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("tools.ci_runner.ROOT", test_root)
    ret = run_deployers(verbose=False)
    assert ret == 1

