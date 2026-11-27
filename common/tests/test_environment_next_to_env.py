"""Each container that receives ENV also receives ENVIRONMENT with the same value.

The locked infra2-sdk reads only ENVIRONMENT; infra2#1823 N1 retired the aliases ENV and
APP_ENV. The SDK Vault template reads `env "ENVIRONMENT"`. A container that has only ENV
gets no environment tier, and a vault-agent renders `secret/data/<project>//<service>`.
This guard fails when a deployed compose service sets ENV without ENVIRONMENT.

Scope: the deployed text of every compose file under the deploy roots: a plain file, or
the compiled text of an x-infra2 source. The compiler writes the vault-agent sidecar,
but the source writes the other services (for example the alerting probe runner, which
gates its watchers on ENV), so the guard reads them after the compile.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from common.deploy.compose_compile import has_x_infra2, load_stack

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_ROOTS = ("bootstrap", "platform", "finance_report", "truealpha")


def _compose_files() -> list[Path]:
    found = []
    for top in COMPOSE_ROOTS:
        for path in sorted((ROOT / top).rglob("*compose*.yaml")):
            if "tests" in path.relative_to(ROOT).parts:
                continue
            found.append(path)
    return found


def _deployed_text(path: Path) -> str:
    return load_stack(path).compiled


def _environment(spec: dict) -> dict[str, str]:
    env = spec.get("environment") or {}
    if isinstance(env, list):
        return dict(item.split("=", 1) if "=" in item else (item, "") for item in env)
    return {key: "" if value is None else str(value) for key, value in env.items()}


def env_without_environment(text: str) -> list[str]:
    """Return each service of ``text`` that sets ENV but not ENVIRONMENT = ENV."""
    doc = yaml.safe_load(text) or {}
    bad = []
    for name, spec in (doc.get("services") or {}).items():
        if not isinstance(spec, dict):
            continue
        env = _environment(spec)
        if "ENV" in env and env.get("ENVIRONMENT") != env["ENV"]:
            bad.append(name)
    return bad


def test_scope_is_not_empty() -> None:
    files = _compose_files()
    # Measured 2026-11-27, after deleting handwritten app preview composes (#1822):
    # 24 files, 13 of them x-infra2 sources.
    assert len(files) >= 24, [str(p.relative_to(ROOT)) for p in files]
    x_infra2 = [p for p in files if has_x_infra2(p.read_text(encoding="utf-8"))]
    assert len(x_infra2) >= 13, [str(p.relative_to(ROOT)) for p in x_infra2]
    with_env = [p for p in files if "ENV: ${ENV" in _deployed_text(p)]
    # The platform sidecars set ENVIRONMENT only, as the compiled sidecar does (#1822
    # step A), and preview stacks moved into x-infra2, so 4 deployed files still set ENV.
    assert len(with_env) >= 4, [str(p.relative_to(ROOT)) for p in with_env]
    assert {"platform/10.authentik", "platform/12.alerting"} <= {
        p.parent.relative_to(ROOT).as_posix() for p in with_env if p in x_infra2
    }


@pytest.mark.parametrize(
    "path", _compose_files(), ids=lambda p: str(p.relative_to(ROOT))
)
def test_every_env_service_sets_environment(path: Path) -> None:
    assert env_without_environment(_deployed_text(path)) == []


def test_guard_rejects_env_only_service() -> None:
    text = "services:\n  agent:\n    environment:\n      ENV: ${ENV}\n"
    assert env_without_environment(text) == ["agent"]


def test_guard_rejects_a_different_value() -> None:
    text = (
        "services:\n  runner:\n    environment:\n"
        "      - ENV=${ENV:?ENV must be explicitly set}\n"
        "      - ENVIRONMENT=${ENV}\n"
    )
    assert env_without_environment(text) == ["runner"]


def test_guard_accepts_the_same_value() -> None:
    text = (
        "services:\n  runner:\n    environment:\n"
        "      - ENV=${ENV:?ENV must be explicitly set}\n"
        "      - ENVIRONMENT=${ENV:?ENV must be explicitly set}\n"
    )
    assert env_without_environment(text) == []
