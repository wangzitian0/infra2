"""`clear_telemetry_env` keeps the shell of a developer out of the app tests (#1209, #1238).

`init_service` reads the tier from `ENVIRONMENT`, `ENV` and `APP_ENV`. Two of them with
different values stop a strict start with `EnvironmentConflictError`. The helper must
remove all three, or a shell that exports `APP_ENV` breaks every test that sets `ENV`.
"""

from __future__ import annotations

import os

import pytest
from infra2_sdk.runtime.environment import (
    EnvironmentTier,
    RuntimeEnvKey,
    runtime_env_spec,
    strict_environment_from_env,
)

from libs.tests.ambient_env import clear_telemetry_env


def _tier_variables() -> tuple[str, ...]:
    """The tier variables as the SDK declares them: the key, then its aliases."""
    spec = runtime_env_spec(RuntimeEnvKey.ENVIRONMENT)
    return (spec.key.value, *spec.aliases)


def test_the_sdk_reads_app_env_as_an_alias_of_the_tier() -> None:
    """The premise of the next tests, taken from the SDK and not from this repository."""
    assert "APP_ENV" in _tier_variables()


def test_the_helper_removes_every_tier_variable_the_sdk_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = _tier_variables()
    for name in names:
        monkeypatch.setenv(name, "staging")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "from-the-shell")

    clear_telemetry_env(monkeypatch)

    assert [name for name in names if name in os.environ] == []
    assert "OTEL_SERVICE_NAME" not in os.environ


def test_a_shell_alias_cannot_conflict_with_the_tier_a_test_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With `APP_ENV=production` left in the shell, `ENV=local_test` raised
    `EnvironmentConflictError` when the strict Canary app loaded."""
    monkeypatch.setenv("APP_ENV", "production")

    clear_telemetry_env(monkeypatch)
    monkeypatch.setenv("ENV", "local_test")

    # GitHub Actions maps `local_test` to `github_ci`; the test states the mapping it checks.
    environment = strict_environment_from_env(github_actions=False)
    assert environment.tier is EnvironmentTier.LOCAL_TEST
