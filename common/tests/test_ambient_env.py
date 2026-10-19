"""`clear_telemetry_env` keeps the telemetry variables of a developer shell out of the app tests.

In its default mode, `init_service` reads the tier, the service name and the version. It checks
each alias pair. Two names of one pair with different values stop a strict start with
`EnvironmentConflictError`, with or without an endpoint. The identity mode of the todo app skips
the tier variables and still checks `SERVICE_VERSION` with `IMAGE_TAG`. The helper removes every
variable and alias that the SDK contract declares, and every name that starts with `OTEL_`.
It does not touch other variables, such as `REDIS_HOST` (#1209, #1238).
These tests take the names from the contract. An SDK release that adds a name needs no edit
here. A release that drops a name checked by
`test_the_contract_declares_the_names_these_tests_rely_on` needs one.
"""

from __future__ import annotations

import os

import pytest
from infra2_sdk.fastapi import init_service
from infra2_sdk.runtime.environment import (
    EnvironmentTier,
    runtime_env_contract,
    strict_environment_from_env,
)

from common.tests import ambient_env
from common.tests.ambient_env import clear_telemetry_env


def _declared() -> dict[str, tuple[str, ...]]:
    """Each variable of the SDK contract with its aliases."""
    return {
        variable["name"]: tuple(variable["aliases"])
        for variable in runtime_env_contract()["variables"]
    }


def _declared_names() -> list[str]:
    """Every variable and every alias of the SDK contract."""
    declared = _declared()
    aliases = {alias for names in declared.values() for alias in names}
    return sorted({*declared, *aliases})


def test_the_contract_declares_the_names_these_tests_rely_on() -> None:
    """The cases below come from the contract. A read that gave no name would leave them
    with nothing to check."""
    declared = _declared()
    assert {"ENV", "APP_ENV"} <= set(declared["ENVIRONMENT"])
    assert "IMAGE_TAG" in declared["SERVICE_VERSION"]


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
        "contract_version": 1,
        "variables": [
            {
                "name": "INFRA2_TEST_VARIABLE",
                "aliases": ["INFRA2_TEST_ALIAS"],
                "sensitive": False,
            }
        ],
    }
    others = [name for name in _declared_names() if not name.startswith("OTEL_")]
    monkeypatch.setattr(ambient_env, "runtime_env_contract", lambda: contract)
    monkeypatch.setenv("INFRA2_TEST_VARIABLE", "one")
    monkeypatch.setenv("INFRA2_TEST_ALIAS", "two")
    for name in others:
        monkeypatch.setenv(name, "kept")

    clear_telemetry_env(monkeypatch)

    assert "INFRA2_TEST_VARIABLE" not in os.environ
    assert "INFRA2_TEST_ALIAS" not in os.environ
    assert [name for name in others if os.environ.get(name) != "kept"] == []


def test_a_shell_alias_cannot_conflict_with_the_tier_a_test_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With `APP_ENV=production` left in the shell, `ENV=local_test` makes
    `strict_environment_from_env` raise `EnvironmentConflictError`."""
    monkeypatch.setenv("APP_ENV", "production")

    clear_telemetry_env(monkeypatch)
    monkeypatch.setenv("ENV", "local_test")

    # GitHub Actions maps `local_test` to `github_ci`; the test states the mapping it checks.
    environment = strict_environment_from_env(github_actions=False)
    assert environment.tier is EnvironmentTier.LOCAL_TEST


class _Providers:
    def shutdown(self) -> None:
        return None


@pytest.mark.parametrize(
    "settings",
    [{}, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:1"}],
    ids=["no-endpoint", "endpoint"],
)
def test_a_shell_version_alias_cannot_stop_a_strict_start(
    monkeypatch: pytest.MonkeyPatch, settings: dict[str, str]
) -> None:
    """`SERVICE_VERSION` and `IMAGE_TAG` with different values stop a strict start with
    `EnvironmentConflictError`. SDK 3.4.0 and later check them also when no endpoint is set."""
    import infra2_sdk.runtime.otel as sdk_otel

    monkeypatch.setattr(
        sdk_otel, "configure_telemetry", lambda _settings, **kw: _Providers()
    )
    monkeypatch.setenv("SERVICE_VERSION", "1.0.0")
    monkeypatch.setenv("IMAGE_TAG", "2.0.0")

    clear_telemetry_env(monkeypatch)
    monkeypatch.setenv("ENV", "local_test")
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    # FastAPI reads OTEL_PROPAGATORS when it is imported, so the import follows the helper.
    from fastapi import FastAPI

    app = FastAPI()
    init_service(app, "ambient-env-test", strict_otel=True)

    assert {"/livez", "/readyz", "/health"} <= {route.path for route in app.routes}
