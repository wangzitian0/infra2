"""Infra-009: deploy_v2 canary orchestration proof.

Tests the canary's orchestration (deploy the reserved preview slot -> health -> teardown)
with deploy_v2 + down monkeypatched — NO live Dokploy/HTTP. The LIVE run is the acceptance
gate and is operator-driven (see the module docstring).
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import pytest

import tools.deploy_v2_canary as canary
from libs.deploy_contract import make_target
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


# --- best-effort teardown (resilience to a flaky control plane) -------------


def test_best_effort_down_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def flaky(kind, value, **kw):
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.HTTPError("transient 502")

    monkeypatch.setattr(canary, "down", flaky)
    ok = canary._best_effort_down(
        domain="z.p", client=object(), _sleep=lambda *_: None
    )
    assert ok is True and calls["n"] == 2


def test_best_effort_down_warns_and_returns_false_on_persistent_failure(
    monkeypatch, capsys
):
    calls = {"n": 0}

    def boom(kind, value, **kw):
        calls["n"] += 1
        raise httpx.HTTPError("502 persists")

    monkeypatch.setattr(canary, "down", boom)
    ok = canary._best_effort_down(
        domain="z.p", client=object(), attempts=3, _sleep=lambda *_: None
    )
    assert ok is False
    assert calls["n"] == 3  # exhausted retries
    err = capsys.readouterr().err
    assert "teardown failed" in err and f"pr-{_CANARY_PR}" in err  # loud leak warning


def test_run_canary_teardown_failure_does_not_mask_deploy_error(monkeypatch, capsys):
    # deploy fast-fails (e.g. composeStatus=error) AND teardown can't clean up: the canary
    # must surface the DEPLOY error (the real cause), not the teardown error, and warn.
    def boom_deploy(**kw):
        raise RuntimeError("deploy failed (composeStatus=error)")

    def boom_down(kind, value, **kw):
        raise httpx.HTTPError("502")

    monkeypatch.setattr(canary, "deploy_v2", boom_deploy)
    monkeypatch.setattr(canary, "down", boom_down)
    monkeypatch.setattr(canary.time, "sleep", lambda *_: None)

    with pytest.raises(RuntimeError, match="composeStatus=error"):
        run_canary(client=object(), domain="z.p", version_ref="main")
    assert "teardown failed" in capsys.readouterr().err


# --- probe alerting: failure classification + out-of-band delivery ----------


def test_failure_domain_classification():
    assert canary.failure_domain(httpx.HTTPError("x")) == "deploy-v2-control-plane"
    assert canary.failure_domain(TimeoutError()) == "deploy-v2-health"
    assert canary.failure_domain(RuntimeError("composeStatus=error")) == "deploy-v2-health"
    assert canary.failure_domain(ValueError("bad ref")) == "deploy-v2-configuration"


class _Args:
    version_ref = "main"
    iac_ref = "main"
    domain = "z.p"


def test_alert_failure_is_best_effort(monkeypatch, capsys):
    import libs.alerting as al

    def boom(env, text, **kw):
        raise RuntimeError("no webhook configured")

    monkeypatch.setattr(al, "deliver_out_of_band_text", boom)
    # must NOT raise — alerting can never crash the probe
    canary.alert_failure({}, domain="deploy-v2-health", detail="x", args=_Args())
    assert "alert delivery failed" in capsys.readouterr().err


def _main_with(monkeypatch, run_impl):
    import libs.alerting as al
    import libs.dokploy as dk

    monkeypatch.setattr(dk, "get_dokploy", lambda host: object())
    monkeypatch.setattr(canary, "run_canary", run_impl)
    sent = {}
    monkeypatch.setattr(
        al, "deliver_out_of_band_text", lambda env, text, **kw: sent.update(text=text)
    )
    return sent


def test_main_alerts_out_of_band_on_failure(monkeypatch):
    def boom(**kw):
        raise httpx.HTTPError("502 Bad Gateway")

    sent = _main_with(monkeypatch, boom)
    rc = canary.main(
        ["--version-ref", "main", "--iac-ref", "main", "--domain", "z.p", "--alert-on-failure"]
    )
    assert rc == 1
    assert "deploy-v2-control-plane" in sent["text"]  # classified + paged


def test_main_no_alert_without_flag(monkeypatch):
    def boom(**kw):
        raise httpx.HTTPError("502")

    sent = _main_with(monkeypatch, boom)
    rc = canary.main(["--version-ref", "main", "--iac-ref", "main", "--domain", "z.p"])
    assert rc == 1 and "text" not in sent  # PR-style run: no out-of-band page


def test_main_treats_leak_as_cleanup_failure(monkeypatch):
    from tools.deploy_v2_canary import CanaryResult

    def leaked(**kw):
        return CanaryResult(
            ok=True, target=_fake_target(), alias="pr-999", url="u",
            healthy=True, torn_down=False,
        )

    sent = _main_with(monkeypatch, leaked)
    rc = canary.main(
        ["--version-ref", "main", "--iac-ref", "main", "--domain", "z.p", "--alert-on-failure"]
    )
    assert rc == 1
    assert "deploy-v2-cleanup" in sent["text"]  # healthy but leaked -> still a failure
