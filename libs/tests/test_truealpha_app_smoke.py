"""truealpha#463 B2 — the post-deploy smoke on AppDeployer.verify_runtime_applied.

Every one of the four 2026-07 truealpha incidents shipped green CI and died only
on the deployed stack. Each test here replays one incident class against the
smoke and requires it to FAIL the deploy:

  #455 class — llm crash-looping     -> /api/health unreachable
  #463 class — route shadowing       -> /api/auth/login 404s
  #447 class — SECRET_KEY missing    -> /api/auth/login 500s
  #461 class — MCP surface dead      -> /api/mcp initialize non-200

plus the green path (all probes healthy -> deploy verified) and the URL
construction for staging vs production.
"""

from __future__ import annotations

import importlib.util
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

_ENV = {"APP_HOST": "truealpha-staging.truealpha.club"}
_GREEN = {"/api/health": 200, "/api/auth/login": 401, "/api/mcp": 200}


@pytest.fixture(scope="module")
def deployer():
    spec = importlib.util.spec_from_file_location(
        "truealpha_app_deploy_smoke", ROOT / "truealpha/truealpha/10.app/deploy.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.AppDeployer


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _install(monkeypatch, deployer, responders: dict[str, object]) -> None:
    """responders: url-suffix -> status int | Exception to raise."""

    def fake_urlopen(request, timeout=None):
        url = request.full_url
        for suffix, outcome in responders.items():
            if url.endswith(suffix):
                if isinstance(outcome, Exception):
                    raise outcome
                return _Response(int(outcome))
        raise AssertionError(f"unexpected probe URL: {url}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(deployer, "env", classmethod(lambda cls: {}))
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    # Single probe pass, then the verdict — no wall-clock in unit tests.
    monkeypatch.setattr(deployer, "SMOKE_DEADLINE_SECONDS", 0)


def test_green_path_passes(deployer, monkeypatch):
    _install(monkeypatch, deployer, dict(_GREEN))
    assert deployer.verify_runtime_applied(None, dict(_ENV)) is None


def test_base_url_construction(deployer, monkeypatch):
    monkeypatch.setattr(deployer, "env", classmethod(lambda cls: {}))
    # Deploy paths that ship APP_HOST win verbatim.
    assert deployer._smoke_base_url(dict(_ENV)) == "https://truealpha-staging.truealpha.club"
    # Fallback recompute uses the truealpha#474 formula: bare domain in
    # production, truealpha<suffix>.<domain> elsewhere.
    assert (
        deployer._smoke_base_url({"ENV": "production", "INTERNAL_DOMAIN": "truealpha.club", "ENV_DOMAIN_SUFFIX": ""})
        == "https://truealpha.club"
    )
    assert (
        deployer._smoke_base_url(
            {"ENV": "staging", "INTERNAL_DOMAIN": "truealpha.club", "ENV_DOMAIN_SUFFIX": "-staging"}
        )
        == "https://truealpha-staging.truealpha.club"
    )


def _expect_failure(deployer, monkeypatch, responders: dict[str, object], needle: str) -> None:
    _install(monkeypatch, deployer, responders)
    err = deployer.verify_runtime_applied(None, dict(_ENV))
    assert err is not None and needle in err, f"expected failure mentioning {needle!r}, got {err!r}"


def test_455_class_llm_unreachable_fails_the_deploy(deployer, monkeypatch):
    _expect_failure(
        deployer,
        monkeypatch,
        {**_GREEN, "/api/health": urllib.error.URLError("connection refused")},
        "GET /api/health",
    )


def test_463_class_login_shadowed_404_fails_the_deploy(deployer, monkeypatch):
    _expect_failure(deployer, monkeypatch, {**_GREEN, "/api/auth/login": 404}, "POST /api/auth/login -> 404")


def test_447_class_login_500_fails_the_deploy(deployer, monkeypatch):
    _expect_failure(deployer, monkeypatch, {**_GREEN, "/api/auth/login": 500}, "POST /api/auth/login -> 500")


def test_461_class_mcp_dead_fails_the_deploy(deployer, monkeypatch):
    _expect_failure(deployer, monkeypatch, {**_GREEN, "/api/mcp": 502}, "POST /api/mcp initialize -> 502")
