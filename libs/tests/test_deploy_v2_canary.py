"""Infra-009: deploy_v2 canary orchestration proof.

Tests the canary's orchestration (deploy preview alias -> health -> teardown) with
deploy_v2 + down monkeypatched — NO live Dokploy/HTTP. The LIVE run is the acceptance
gate and is operator-driven (see the module docstring).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

import tools.deploy_v2_canary as canary
from tools.deploy_contract import make_deploy_target
from tools.deploy_v2_canary import run_canary

SHA_CODE = "e" * 40
SHA_IAC = "f" * 40


def _fake_target():
    return make_deploy_target(
        service="finance_report/app",
        env="preview",
        code_version=SHA_CODE,
        iac_ref=SHA_IAC,
        alias_kind="pr",
        alias_value=999,
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
        return _DV2Result(
            target=_fake_target(),
            data_lane="staging",
            backend="preview-lifecycle",
            detail={
                "alias": "pr-999",
                "compose_id": "cmp",
                "sha": kw["code_version"],
                "url": f"https://report-pr-999.{kw['domain']}",
                "healthy": True,
            },
        )

    def fake_down(kind, value, **kw):
        rec["down"] = {"kind": kind, "value": value, **kw}

    monkeypatch.setattr(canary, "deploy_v2", fake_deploy_v2)
    monkeypatch.setattr(canary, "down", fake_down)
    return rec


def test_canary_deploys_preview_then_tears_down(spies):
    res = run_canary(
        client=object(), domain="zitian.party", code_version=SHA_CODE, iac_ref=SHA_IAC
    )
    assert res.ok is True
    assert res.alias == "pr-999"
    assert res.torn_down is True
    # deployed the canary preview alias via the unified front door...
    assert spies["deploy"]["env"] == "preview"
    assert (
        spies["deploy"]["alias_kind"] == "pr" and spies["deploy"]["alias_value"] == 999
    )
    # ...then tore that same alias down.
    assert spies["down"]["kind"] == "pr"
    assert spies["down"]["value"] == 999
    assert spies["down"]["domain"] == "zitian.party"


def test_keep_skips_teardown(spies):
    res = run_canary(
        client=object(),
        domain="zitian.party",
        code_version=SHA_CODE,
        iac_ref=SHA_IAC,
        teardown=False,
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
        run_canary(
            client=object(),
            domain="zitian.party",
            code_version=SHA_CODE,
            iac_ref=SHA_IAC,
        )
    assert rec["down"] == ("pr", 999)  # cleanup still ran


def test_custom_pr_number(spies):
    run_canary(
        client=object(),
        domain="zitian.party",
        code_version=SHA_CODE,
        iac_ref=SHA_IAC,
        pr_number=12345,
    )
    assert spies["deploy"]["alias_value"] == 12345
    assert spies["down"]["value"] == 12345
