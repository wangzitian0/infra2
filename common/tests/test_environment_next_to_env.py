"""Each container that receives ENV also receives ENVIRONMENT with the same value.

The locked infra2-sdk reads only ENVIRONMENT; infra2#1823 N1 retired the aliases ENV and
APP_ENV. The SDK Vault template reads `env "ENVIRONMENT"`. A container that has only ENV
gets no environment tier, and a vault-agent renders `secret/data/<project>//<service>`.
This guard fails when a hand-written compose service sets ENV without ENVIRONMENT.

Scope: every plain compose file under the deploy roots. A compiled x-infra2 stack gets
its vault-agent from the infra2-sdk compiler, so the SDK tests govern that sidecar.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from common.deploy.compose_compile import has_x_infra2

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_ROOTS = ("bootstrap", "platform", "finance_report", "truealpha")


def _plain_compose_files() -> list[Path]:
    found = []
    for top in COMPOSE_ROOTS:
        for path in sorted((ROOT / top).rglob("*compose*.yaml")):
            if "tests" in path.relative_to(ROOT).parts:
                continue
            if not has_x_infra2(path.read_text(encoding="utf-8")):
                found.append(path)
    return found


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
    files = _plain_compose_files()
    # 14 plain files on 2026-11-26 (measured), after platform/10.authentik moved to
    # x-infra2 (#1822 step B).
    assert len(files) >= 14, [str(p.relative_to(ROOT)) for p in files]
    with_env = [p for p in files if "ENV: ${ENV" in p.read_text(encoding="utf-8")]
    # The platform sidecars set ENVIRONMENT only, as the compiled sidecar does (#1822
    # step A), so fewer files set ENV. 5 files still set it (measured 2026-11-26, after
    # platform/10.authentik moved to x-infra2).
    assert len(with_env) >= 5, [str(p.relative_to(ROOT)) for p in with_env]


@pytest.mark.parametrize(
    "path", _plain_compose_files(), ids=lambda p: str(p.relative_to(ROOT))
)
def test_every_env_service_sets_environment(path: Path) -> None:
    assert env_without_environment(path.read_text(encoding="utf-8")) == []


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
