"""libs.core.environ: get_env() fails closed when no environment is chosen (#1039).

The process picks its deployment environment with INFRA_ENVIRONMENT or DEPLOY_ENV.
A bare operator command must never reach production: when neither variable holds a
value, get_env() raises. Production is chosen explicitly like any other environment.
"""

from __future__ import annotations

import os

import pytest

import libs.core.environ as common


class _FakeOp:
    def get(self, key):
        return None


@pytest.fixture(autouse=True)
def _no_one_password_read(monkeypatch):
    """No 1Password read and no PROJECT override. conftest.py clears the environment."""
    monkeypatch.setattr("libs.security.store.OpSecrets", lambda *a, **k: _FakeOp())
    monkeypatch.delenv("PROJECT", raising=False)


def test_set_deploy_env_resets_the_memoized_config(monkeypatch) -> None:
    monkeypatch.setenv("DEPLOY_ENV", "production")
    assert common.get_env()["ENV"] == "production"
    common.set_deploy_env("staging")
    env = common.get_env()
    assert (env["ENV"], env["ENV_SUFFIX"], env["ENV_DOMAIN_SUFFIX"]) == (
        "staging",
        "-staging",
        "-staging",
    )
    common.set_deploy_env("production")
    assert (common.get_env()["ENV"], common.get_env()["ENV_SUFFIX"]) == (
        "production",
        "",
    )


def test_get_env_without_any_environment_variable_raises_and_names_both() -> None:
    with pytest.raises(ValueError) as raised:
        common.get_env()
    message = str(raised.value)
    assert "INFRA_ENVIRONMENT" in message
    assert "DEPLOY_ENV" in message
    assert "production" in message
    assert "explicit" in message.lower()


def test_the_missing_environment_error_is_a_dedicated_type() -> None:
    with pytest.raises(common.EnvironmentNotSetError):
        common.get_env()


@pytest.mark.parametrize(
    ("infra_environment", "deploy_env"),
    [
        ("", ""),
        ("   ", ""),
        ("", " \t "),
        ("  ", "  "),
    ],
)
def test_blank_values_count_as_unset(
    monkeypatch, infra_environment: str, deploy_env: str
) -> None:
    monkeypatch.setenv("INFRA_ENVIRONMENT", infra_environment)
    monkeypatch.setenv("DEPLOY_ENV", deploy_env)
    with pytest.raises(common.EnvironmentNotSetError):
        common.get_env()


def test_blank_infra_environment_falls_through_to_deploy_env(monkeypatch) -> None:
    monkeypatch.setenv("INFRA_ENVIRONMENT", "  ")
    monkeypatch.setenv("DEPLOY_ENV", "staging")
    assert common.get_env()["ENV"] == "staging"


def test_a_failed_get_env_is_not_memoized(monkeypatch) -> None:
    with pytest.raises(common.EnvironmentNotSetError):
        common.get_env()
    monkeypatch.setenv("DEPLOY_ENV", "staging")
    assert common.get_env()["ENV"] == "staging"


@pytest.mark.parametrize("variable", ["INFRA_ENVIRONMENT", "DEPLOY_ENV"])
@pytest.mark.parametrize("value", ["production", "prod", " PRODUCTION "])
def test_production_chosen_explicitly_still_works(
    monkeypatch, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)
    env = common.get_env()
    assert env["ENV"] == "production"
    assert env["ENV_DOMAIN_SUFFIX"] == ""
    assert env["ENV_SUFFIX"] == ""


@pytest.mark.parametrize("variable", ["INFRA_ENVIRONMENT", "DEPLOY_ENV"])
def test_staging_still_works(monkeypatch, variable: str) -> None:
    monkeypatch.setenv(variable, "staging")
    env = common.get_env()
    assert env["ENV"] == "staging"
    assert env["ENV_DOMAIN_SUFFIX"] == "-staging"
    assert env["ENV_SUFFIX"] == "-staging"


def test_infra_environment_wins_over_deploy_env(monkeypatch) -> None:
    monkeypatch.setenv("INFRA_ENVIRONMENT", "staging")
    monkeypatch.setenv("DEPLOY_ENV", "production")
    assert common.get_env()["ENV"] == "staging"


def test_an_explicit_name_needs_no_process_variable() -> None:
    env = common.get_env("staging")
    assert env["ENV"] == "staging"
    assert env["ENV_SUFFIX"] == "-staging"
    assert env["ENV_DOMAIN_SUFFIX"] == "-staging"
    assert "INFRA_ENVIRONMENT" not in os.environ
    assert "DEPLOY_ENV" not in os.environ


def test_an_explicit_name_wins_over_the_process_variables(monkeypatch) -> None:
    monkeypatch.setenv("INFRA_ENVIRONMENT", "production")
    monkeypatch.setenv("ENV_SUFFIX", "")
    env = common.get_env("staging")
    assert env["ENV"] == "staging"
    assert env["ENV_SUFFIX"] == "-staging"


def test_an_explicit_name_does_not_replace_the_process_environment(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DEPLOY_ENV", "production")
    assert common.get_env("staging")["ENV"] == "staging"
    assert common.get_env()["ENV"] == "production"
    assert common.get_env("staging")["ENV"] == "staging"


@pytest.mark.parametrize("name", ["", "   "])
def test_a_blank_explicit_name_raises_even_when_the_process_has_one(
    monkeypatch, name: str
) -> None:
    monkeypatch.setenv("DEPLOY_ENV", "production")
    with pytest.raises(common.EnvironmentNotSetError):
        common.get_env(name)


@pytest.mark.parametrize("name", ["", "   "])
def test_set_deploy_env_rejects_a_blank_name(monkeypatch, name: str) -> None:
    with pytest.raises(common.EnvironmentNotSetError):
        common.set_deploy_env(name)
    assert "INFRA_ENVIRONMENT" not in os.environ
    assert "DEPLOY_ENV" not in os.environ
