"""`clear_telemetry_env` keeps the telemetry variables of a developer shell out of the app tests.

In its default mode, `init_service` reads the tier, the service name and the version.
The infra2-sdk retires the aliases `ENV`, `APP_ENV` and `IMAGE_TAG` (infra2#1823 N1): the SDK
does not read them, so a value of a retired name in the shell cannot stop a strict start.
The helper removes every variable and alias that the SDK contract declares, and every name
that starts with `OTEL_`.
It does not touch other variables, such as `REDIS_HOST` (#1209, #1238).
These tests take the names from the contract. An SDK release that adds a name needs no edit
here. A release that drops a name checked by
`test_the_contract_declares_the_names_these_tests_rely_on` needs one.
"""

from __future__ import annotations

import os

import pytest
from infra2_sdk.config import (
    EnvironmentTier,
    resolve_environment,
    shared_env_contract,
)
from infra2_sdk.service import init_service

from common.tests import ambient_env
from common.tests.ambient_env import clear_telemetry_env


def _declared() -> dict[str, tuple[str, ...]]:
    """Each variable of the SDK contract with its aliases."""
    return {
        variable["name"]: tuple(variable.get("aliases") or ())
        for variable in shared_env_contract()["groups"]["runtime"]
    }


def _declared_names() -> list[str]:
    """Every variable and every alias of the SDK contract."""
    declared = _declared()
    aliases = {alias for names in declared.values() for alias in names}
    return sorted({*declared, *aliases})


_RETIRED = ("ENV", "APP_ENV", "IMAGE_TAG")


def test_the_contract_declares_the_names_these_tests_rely_on() -> None:
    """The cases below come from the contract. A read that gave no name would leave them
    with nothing to check. The contract declares no retired name, not even as an alias."""
    declared = _declared()
    assert {"ENVIRONMENT", "SERVICE_VERSION"} <= set(declared)
    assert [name for name in _RETIRED if name in _declared_names()] == []


@pytest.mark.parametrize("name", [*_declared_names(), "OTEL_NOT_IN_THE_CONTRACT"])
def test_the_helper_removes_the_name(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "from-the-shell")

    clear_telemetry_env(monkeypatch)

    assert name not in os.environ


def test_the_helper_reads_the_contract_and_keeps_no_list_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An SDK release can add or drop names. The helper must follow the contract it gets.
    A name that this contract does not declare stays."""
    contract = {
        "schema_version": 2,
        "groups": {
            "runtime": [
                {
                    "name": "INFRA2_TEST_VARIABLE",
                    "aliases": ["INFRA2_TEST_ALIAS"],
                    "sensitive": False,
                }
            ]
        },
    }
    others = [name for name in _declared_names() if not name.startswith("OTEL_")]
    monkeypatch.setattr(ambient_env, "shared_env_contract", lambda: contract)
    monkeypatch.setenv("INFRA2_TEST_VARIABLE", "one")
    monkeypatch.setenv("INFRA2_TEST_ALIAS", "two")
    for name in others:
        monkeypatch.setenv(name, "kept")

    clear_telemetry_env(monkeypatch)

    assert "INFRA2_TEST_VARIABLE" not in os.environ
    assert "INFRA2_TEST_ALIAS" not in os.environ
    assert [name for name in others if os.environ.get(name) != "kept"] == []


def test_a_retired_shell_alias_cannot_change_the_tier_a_test_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With `APP_ENV=production` and `ENV=production` left in the shell,
    `ENVIRONMENT=local_test` gives the tier `local_test`: the SDK reads no retired name."""
    clear_telemetry_env(monkeypatch)
    # The helper removes only contract names, so the retired names stay in the shell.
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("ENVIRONMENT", "local_test")

    # GitHub Actions maps `local_test` to `github_ci`; the test states the mapping it checks.
    environment = resolve_environment({"ENVIRONMENT": "local_test"})
    assert environment.tier is EnvironmentTier.LOCAL_TEST


class _Providers:
    tracer_provider = None
    meter_provider = None
    logger_provider = None

    def shutdown(self) -> None:
        return None


@pytest.mark.parametrize(
    "settings",
    [{}, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:1"}],
    ids=["no-endpoint", "endpoint"],
)
def test_a_retired_shell_version_alias_cannot_stop_a_strict_start(
    monkeypatch: pytest.MonkeyPatch, settings: dict[str, str]
) -> None:
    """`SERVICE_VERSION` and the retired `IMAGE_TAG` with different values do not stop a
    strict start, with or without an endpoint: the SDK does not read `IMAGE_TAG`."""
    import infra2_sdk.telemetry as sdk_otel

    monkeypatch.setattr(
        sdk_otel, "configure_telemetry", lambda _settings, **kw: _Providers()
    )
    clear_telemetry_env(monkeypatch)
    monkeypatch.setenv("IMAGE_TAG", "2.0.0")
    monkeypatch.setenv("SERVICE_VERSION", "1.0.0")
    monkeypatch.setenv("ENVIRONMENT", "local_test")
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    # FastAPI reads OTEL_PROPAGATORS when it is imported, so the import follows the helper.
    from fastapi import FastAPI

    app = FastAPI()
    init_service(app, "ambient-env-test", strict_otel=True)

    assert {"/livez", "/readyz", "/health"} <= {route.path for route in app.routes}
