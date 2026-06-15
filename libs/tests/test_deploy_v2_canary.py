"""Infra-009: deploy_v2 canary orchestration proof.

Tests the canary's orchestration (deploy the reserved preview slot -> health -> teardown)
with deploy_v2 + down monkeypatched — NO live Dokploy/HTTP. The LIVE run is the acceptance
gate and is operator-driven (see the module docstring).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

import tools.deploy_v2_canary as canary
from tools.deploy_contract import make_target
from tools.deploy_v2_canary import _CANARY_PR, run_canary

SHA_CODE = "e" * 40
SHA_IAC = "f" * 40


def _fake_target():
    return make_target(
        "canary",
        service="finance_report/app",
        version=SHA_CODE,
        iac_ref=SHA_IAC,
        alias_value=_CANARY_PR,
    )


@dataclass
class _DV2Result:
    target: object
    data_lane: str
    backend: str
    detail: dict


@pytest.fixture
def spies(monkeypatch):
    rec = {"deploy": None, "down": None}

    def fake_deploy_v2(**kw):
        rec["deploy"] = kw
        # Mirror the real backend: preview_lifecycle.up(wait=False) returns healthy=None,
        # so the spy must too — otherwise the wait=False path can't be exercised.
        healthy = True if kw.get("wait", True) else None
        return _DV2Result(
            target=_fake_target(),
            data_lane="staging",
            backend="preview-lifecycle",
            detail={
                "alias": f"pr-{_CANARY_PR}",
                "compose_id": "cmp",
                "sha": SHA_CODE,
                "url": f"https://report-pr-{_CANARY_PR}.{kw['domain']}",
                "healthy": healthy,
            },
        )

    def fake_down(kind, value, **kw):
        rec["down"] = {"kind": kind, "value": value, **kw}

    monkeypatch.setattr(canary, "deploy_v2", fake_deploy_v2)
    monkeypatch.setattr(canary, "down", fake_down)
    return rec


def test_canary_deploys_reserved_slot_then_tears_down(spies):
    res = run_canary(client=object(), domain="zitian.party", version_ref="main")
    assert res.ok is True
    assert res.alias == f"pr-{_CANARY_PR}"
    assert res.torn_down is True
    # deployed via the unified front door with the canary type...
    assert spies["deploy"]["deploy_type"] == "canary"
    assert spies["deploy"]["version_ref"] == "main"
    # ...then tore the reserved slot down.
    assert spies["down"]["kind"] == "pr"
    assert spies["down"]["value"] == _CANARY_PR
    assert spies["down"]["domain"] == "zitian.party"


def test_keep_skips_teardown(spies):
    res = run_canary(
        client=object(), domain="zitian.party", version_ref="main", teardown=False
    )
    assert res.torn_down is False
    assert spies["down"] is None


def test_teardown_runs_even_when_deploy_raises(monkeypatch):
    rec = {"down": None}

    def boom(**kw):
        raise TimeoutError("never went healthy")

    def fake_down(kind, value, **kw):
        rec["down"] = (kind, value)

    monkeypatch.setattr(canary, "deploy_v2", boom)
    monkeypatch.setattr(canary, "down", fake_down)

    with pytest.raises(TimeoutError):
        run_canary(client=object(), domain="zitian.party", version_ref="main")
    assert rec["down"] == ("pr", _CANARY_PR)  # cleanup still ran


def test_no_wait_reports_unknown_health_but_still_tears_down(spies):
    # --no-wait: the deploy is fire-and-forget, so health is unknown (ok/healthy None),
    # but teardown still runs by default so the canary never leaks its ephemeral stack.
    res = run_canary(
        client=object(), domain="zitian.party", version_ref="main", wait=False
    )
    assert res.ok is None
    assert res.healthy is None
    assert res.torn_down is True
    assert spies["deploy"]["wait"] is False
    assert spies["down"]["value"] == _CANARY_PR


def test_version_ref_forwarded(spies):
    run_canary(client=object(), domain="zitian.party", version_ref="v2.0.0")
    assert spies["deploy"]["version_ref"] == "v2.0.0"
    assert spies["down"]["value"] == _CANARY_PR  # slot stays fixed regardless of code
