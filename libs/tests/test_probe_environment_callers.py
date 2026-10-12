"""The four probe modules take their environment from one resolver (#1208).

Each module used to resolve the deployment environment alone, with its own variable
names and precedence. Three of them defaulted to production. A probe that cannot tell
its environment must stop: it must not report itself as production.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

import libs.observability.probes as probes
from libs.observability.probe_dispatch import _chronic_digest_payload
from tools import observability_roundtrip_probe as roundtrip

ROOT = Path(__file__).resolve().parents[2]

# Every variable that any of the four modules read before #1208.
_PROBE_ENVIRONMENT_VARIABLES = (
    "INFRA_ENVIRONMENT",
    "INFRA_PROBE_HEARTBEAT_ENV",
    "ENV",
    "DEPLOY_ENV",
    "OBS_ROUNDTRIP_ENV",
)


def _load_probe_runner():
    path = ROOT / "tools/infra_probe_runner.py"
    spec = importlib.util.spec_from_file_location("infra_probe_runner_1208", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _no_probe_environment(monkeypatch):
    """Start each test with no environment variable set, whatever the shell holds."""
    for name in _PROBE_ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def _failed_vault_result():
    spec = probes.parse_probe_specs(
        "vault-http|http|http://vault:8200/v1/sys/health|200|critical|5||platform/vault"
    )[0]
    return probes.run_probe(spec, http_get=lambda *_args: (503, "sealed"))


# ---- libs/observability/probes.py: build_probe_alert_payload -------------------------


def test_alert_payload_without_an_environment_raises_and_does_not_say_production():
    with pytest.raises(ValueError, match="INFRA_ENVIRONMENT"):
        probes.build_probe_alert_payload([_failed_vault_result()])


def test_alert_payload_labels_the_environment_it_runs_in(monkeypatch):
    monkeypatch.setenv("ENV", "staging")

    (alert,) = probes.build_probe_alert_payload([_failed_vault_result()])["alerts"]

    assert alert["labels"]["environment"] == "staging"


def test_alert_payload_labels_a_prod_alias_as_production(monkeypatch):
    monkeypatch.setenv("ENV", "prod")

    (alert,) = probes.build_probe_alert_payload([_failed_vault_result()])["alerts"]

    assert alert["labels"]["environment"] == "production"


# ---- libs/observability/probe_dispatch.py: _chronic_digest_payload -------------------

_CHRONIC = [("public-routes", 1_000.0, ["vault-public-route"])]


def test_chronic_digest_without_an_environment_raises_and_does_not_say_prod():
    with pytest.raises(ValueError, match="DEPLOY_ENV"):
        _chronic_digest_payload(_CHRONIC, 200_000.0)


def test_chronic_digest_labels_the_environment_it_runs_in(monkeypatch):
    monkeypatch.setenv("ENV", "staging")

    payload = _chronic_digest_payload(_CHRONIC, 200_000.0)

    assert payload["alerts"][0]["labels"]["environment"] == "staging"


def test_chronic_digest_keeps_an_explicit_environment_argument(monkeypatch):
    """The runner passes its own environment. The caller choice wins over the process."""
    monkeypatch.setenv("ENV", "staging")

    payload = _chronic_digest_payload(_CHRONIC, 200_000.0, environment="production")

    assert payload["alerts"][0]["labels"]["environment"] == "production"


# ---- tools/infra_probe_runner.py: _deploy_env, host specs, main ----------------------


def test_runner_without_an_environment_raises_and_does_not_say_production():
    runner = _load_probe_runner()

    with pytest.raises(ValueError, match="INFRA_PROBE_HEARTBEAT_ENV"):
        runner._deploy_env()


def test_runner_reports_staging_when_env_is_staging(monkeypatch):
    runner = _load_probe_runner()
    monkeypatch.setenv("ENV", "staging")

    assert runner._deploy_env() == "staging"


def test_runner_host_probe_gate_raises_without_an_environment():
    """The gate used to keep host probes on when the environment was unknown."""
    runner = _load_probe_runner()
    specs = probes.parse_probe_specs("host-cpu|resource|cpu|80")

    with pytest.raises(ValueError, match="ENV"):
        runner._host_specs_for_env(specs)


def test_runner_delivery_gate_raises_without_an_environment():
    """The pager gate used to page when the environment was unknown."""
    runner = _load_probe_runner()

    with pytest.raises(ValueError, match="ENV"):
        runner._delivers_as_report()


def test_runner_main_exits_with_config_error_when_no_environment(
    monkeypatch, tmp_path, capsys
):
    runner = _load_probe_runner()
    monkeypatch.setattr(sys, "argv", ["infra_probe_runner.py", "--once"])
    monkeypatch.setenv("ALERTING_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("INFRA_PROBE_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(
        runner,
        "run_once",
        lambda **_kwargs: pytest.fail("the runner must stop before it probes"),
    )

    assert runner.main() == os.EX_CONFIG

    assert "ENV" in capsys.readouterr().err


def test_runner_help_works_without_an_environment(monkeypatch, capsys):
    """Resolution is lazy: a CLI `--help` must not need an environment."""
    runner = _load_probe_runner()
    monkeypatch.setattr(sys, "argv", ["infra_probe_runner.py", "--help"])

    with pytest.raises(SystemExit) as exit_info:
        runner.main()

    assert exit_info.value.code == 0
    assert "--loop" in capsys.readouterr().out


# ---- tools/observability_roundtrip_probe.py: _deploy_env, main -----------------------


def test_roundtrip_without_an_environment_raises_a_config_error():
    with pytest.raises(roundtrip.ProbeConfigError, match="OBS_ROUNDTRIP_ENV"):
        roundtrip._deploy_env()


def test_roundtrip_reports_staging_when_env_is_staging(monkeypatch):
    monkeypatch.setenv("ENV", "staging")

    assert roundtrip._deploy_env() == "staging"


def test_roundtrip_override_variable_wins_over_the_shared_variables(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("OBS_ROUNDTRIP_ENV", "staging")

    assert roundtrip._deploy_env() == "staging"


def test_roundtrip_main_reports_misconfigured_not_failed_without_an_environment(
    monkeypatch, tmp_path, capsys
):
    """A probe that cannot tell its environment tested nothing. The runner keeps that
    exit code out of the failing stream, so the missing variable cannot page."""
    monkeypatch.setattr(
        sys, "argv", ["observability_roundtrip_probe.py", "signoz", "--force"]
    )
    monkeypatch.setenv("ALERTING_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("OBS_ROUNDTRIP_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(
        roundtrip,
        "_post_json",
        lambda *_args, **_kwargs: pytest.fail("the probe must stop before it posts"),
    )

    assert roundtrip.main() == roundtrip.MISCONFIGURED_EXIT_CODE

    assert "roundtrip-misconfigured" in capsys.readouterr().err
