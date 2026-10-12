"""Tests for the one probe-environment resolver (#1208)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from libs.core.environ import EnvironmentNotSetError
from libs.observability.probe_environment import (
    PROBE_ENVIRONMENT_VARIABLES,
    resolve_probe_environment,
)
from libs.tests.compose_env import container_env

ROOT = Path(__file__).resolve().parents[2]
ALERTING_COMPOSE = ROOT / "platform/12.alerting/compose.yaml"

# The names that choose a probe environment, in precedence order, and one valid value
# for each. The values differ, so a test sees which variable won.
_ORDERED_VALUES = (
    ("INFRA_ENVIRONMENT", "pr-1", "pr_1"),
    ("INFRA_PROBE_HEARTBEAT_ENV", "preview", "preview"),
    ("ENV", "staging", "staging"),
    ("DEPLOY_ENV", "production", "production"),
)


@pytest.fixture(autouse=True)
def _no_probe_environment(monkeypatch):
    for name in (*PROBE_ENVIRONMENT_VARIABLES, "OBS_ROUNDTRIP_ENV"):
        monkeypatch.delenv(name, raising=False)


def test_the_accepted_variables_are_the_ones_in_the_precedence_table():
    assert PROBE_ENVIRONMENT_VARIABLES == tuple(name for name, *_ in _ORDERED_VALUES)


@pytest.mark.parametrize("name", PROBE_ENVIRONMENT_VARIABLES)
def test_each_accepted_variable_alone_chooses_the_environment(monkeypatch, name):
    monkeypatch.setenv(name, "staging")

    assert resolve_probe_environment() == "staging"


@pytest.mark.parametrize("first", range(len(_ORDERED_VALUES)))
def test_an_earlier_variable_wins_over_every_later_one(monkeypatch, first):
    for name, raw, _normalized in _ORDERED_VALUES[first:]:
        monkeypatch.setenv(name, raw)

    assert resolve_probe_environment() == _ORDERED_VALUES[first][2]


def test_an_override_variable_wins_over_the_shared_variables(monkeypatch):
    monkeypatch.setenv("OBS_ROUNDTRIP_ENV", "staging")
    monkeypatch.setenv("ENV", "production")

    assert resolve_probe_environment("OBS_ROUNDTRIP_ENV") == "staging"
    assert resolve_probe_environment() == "production"


def test_an_unset_override_falls_through_to_the_shared_variables(monkeypatch):
    monkeypatch.setenv("ENV", "staging")

    assert resolve_probe_environment("OBS_ROUNDTRIP_ENV") == "staging"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("prod", "production"),
        ("PRODUCTION ", "production"),
        ("production", "production"),
        ("Staging", "staging"),
        (" stg ", "staging"),
        ("preview", "preview"),
        ("pr-5", "pr_5"),
        ("branch-main", "branch_main"),
        ("commit-1ab32d5", "commit_1ab32d5"),
        ("tag-v1.2.3", "tag_v1.2.3"),
        ("canary-preview", "canary_preview"),
    ],
)
def test_a_value_is_normalized_to_the_name_the_estate_uses(monkeypatch, raw, expected):
    monkeypatch.setenv("ENV", raw)

    assert resolve_probe_environment() == expected


def test_a_blank_variable_counts_as_unset(monkeypatch):
    monkeypatch.setenv("INFRA_ENVIRONMENT", "   ")
    monkeypatch.setenv("INFRA_PROBE_HEARTBEAT_ENV", "")
    monkeypatch.setenv("DEPLOY_ENV", "staging")

    assert resolve_probe_environment() == "staging"


def test_no_variable_raises_and_names_every_variable():
    with pytest.raises(EnvironmentNotSetError) as raised:
        resolve_probe_environment("OBS_ROUNDTRIP_ENV")

    assert isinstance(raised.value, ValueError)
    message = str(raised.value)
    for name in ("OBS_ROUNDTRIP_ENV", *PROBE_ENVIRONMENT_VARIABLES):
        assert name in message
    assert "never the default" in message


def test_only_blank_variables_raise(monkeypatch):
    for name in PROBE_ENVIRONMENT_VARIABLES:
        monkeypatch.setenv(name, "  ")

    with pytest.raises(EnvironmentNotSetError):
        resolve_probe_environment()


@pytest.mark.parametrize("raw", ["garbage", "dev", "qa", "productionn", "a b", "a/b"])
def test_an_unknown_value_raises_and_names_the_variable_and_the_value(monkeypatch, raw):
    monkeypatch.setenv("DEPLOY_ENV", raw)

    with pytest.raises(ValueError, match="DEPLOY_ENV") as raised:
        resolve_probe_environment()

    assert repr(raw) in str(raised.value)
    assert "production" in str(raised.value)


def test_the_first_set_variable_decides_even_when_its_value_is_unknown(monkeypatch):
    """A bad value must not hide behind a later good one: that would mask a typo."""
    monkeypatch.setenv("INFRA_ENVIRONMENT", "garbage")
    monkeypatch.setenv("ENV", "staging")

    with pytest.raises(ValueError, match="INFRA_ENVIRONMENT"):
        resolve_probe_environment()


# ---- The deployed container still starts ----------------------------------------------


@pytest.mark.parametrize(
    ("deployed_env", "env_suffix"), [("production", ""), ("staging", "-staging")]
)
def test_the_deployed_probe_container_resolves_its_environment(
    monkeypatch, deployed_env, env_suffix
):
    """The deploy pushes ENV. The compose file gives the probe container its variables.
    The runner and the round-trip probe run in that container, so both must resolve."""
    container = container_env(
        ALERTING_COMPOSE,
        "infra-probe-runner",
        {"ENV": deployed_env, "ENV_SUFFIX": env_suffix},
    )
    accepted = {k: v for k, v in container.items() if k in PROBE_ENVIRONMENT_VARIABLES}
    assert accepted, "the container sets none of the variables the resolver reads"
    for name, value in accepted.items():
        monkeypatch.setenv(name, value)

    assert resolve_probe_environment() == deployed_env
    assert resolve_probe_environment("OBS_ROUNDTRIP_ENV") == deployed_env


# ---- The guard: no probe module resolves the environment on its own ------------------

_ENVIRONMENT_NAMES = frozenset({*PROBE_ENVIRONMENT_VARIABLES, "OBS_ROUNDTRIP_ENV"})


def _own_environment_reads(source: str) -> list[tuple[int, str]]:
    """(line, name) for each os.getenv / os.environ.get / os.environ[...] of a name."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        key = None
        if isinstance(node, ast.Call) and node.args:
            func = ast.unparse(node.func)
            if func in {"os.getenv", "os.environ.get"}:
                key = node.args[0]
        elif (
            isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ"
        ):
            key = node.slice
        if isinstance(key, ast.Constant) and key.value in _ENVIRONMENT_NAMES:
            found.append((node.lineno, key.value))
    return found


def _governed_files() -> list[Path]:
    """Every module that must take its probe environment from the resolver."""
    library = [
        path
        for path in sorted((ROOT / "libs/observability").rglob("*.py"))
        if path.name != "probe_environment.py"
    ]
    return [
        *library,
        ROOT / "tools/infra_probe_runner.py",
        ROOT / "tools/observability_roundtrip_probe.py",
    ]


def test_the_detector_finds_a_module_that_resolves_the_environment_alone():
    """Positive control: the guard below must fail on the pre-#1208 code shapes."""
    assert _own_environment_reads(
        'x = os.getenv("INFRA_ENVIRONMENT") or "production"'
    ) == [(1, "INFRA_ENVIRONMENT")]
    assert _own_environment_reads('x = os.environ.get("ENV", "prod")') == [(1, "ENV")]
    assert _own_environment_reads('x = os.environ["DEPLOY_ENV"]') == [(1, "DEPLOY_ENV")]
    assert _own_environment_reads('x = os.getenv("OBS_ROUNDTRIP_STATE_FILE")') == []


def test_no_probe_module_resolves_the_environment_on_its_own():
    files = _governed_files()
    assert len(files) > 10, files  # a guard that scans nothing proves nothing
    offenders = {
        f"{path.relative_to(ROOT)}:{line} reads {name}"
        for path in files
        for line, name in _own_environment_reads(path.read_text(encoding="utf-8"))
    }
    assert not offenders, sorted(offenders)
