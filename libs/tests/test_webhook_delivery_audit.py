"""tools/webhook_delivery_audit: attempted-but-never-landing is the signal (#682).

`/webhook` returned 401 on every delivery for seven weeks (#585) while the one record of
it — the hook's own delivery list — went unread. These tests hold the check to the narrow
shape that makes it worth reading: a quiet hook is not a finding, one bad delivery among
good ones is not a finding, and a hook that lands nothing is.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _module():
    spec = importlib.util.spec_from_file_location(
        "webhook_delivery_audit", ROOT / "tools/webhook_delivery_audit.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["webhook_delivery_audit"] = module
    spec.loader.exec_module(module)
    return module


def _fake_get(hooks, deliveries):
    def get(path: str, _token: str):
        if path.endswith("/hooks"):
            return hooks
        return deliveries[int(path.rsplit("/", 2)[-2])]

    return get


def test_a_hook_that_lands_nothing_is_the_finding():
    """#585's shape: every delivery attempted, every one refused."""
    audit = _module()
    hooks = [
        {
            "id": 1,
            "config": {"url": "https://iac/webhook"},
            "updated_at": "2026-01-10T17:38:50Z",
        }
    ]
    deliveries = {1: [{"status_code": 401} for _ in range(12)]}

    [verdict] = audit.verdicts("o/r", "t", get=_fake_get(hooks, deliveries))

    assert verdict.failing
    assert "NONE LANDED" in verdict.line()
    # the shape of the fault is a secret rotated on one side only, so say when GitHub's
    # copy was last written
    assert "2026-01-10T17:38:50Z" in verdict.line()


def test_one_bad_delivery_among_good_ones_is_not_a_finding():
    audit = _module()
    hooks = [{"id": 1, "config": {"url": "https://iac/webhook"}, "updated_at": ""}]
    deliveries = {1: [{"status_code": 200}, {"status_code": 500}, {"status_code": 200}]}

    [verdict] = audit.verdicts("o/r", "t", get=_fake_get(hooks, deliveries))

    assert not verdict.failing
    assert "2/3 ok" in verdict.line()


def test_a_quiet_hook_is_not_a_finding():
    """A day with no pushes must not page."""
    audit = _module()
    hooks = [{"id": 7, "config": {"url": "https://iac/webhook"}, "updated_at": ""}]

    [verdict] = audit.verdicts("o/r", "t", get=_fake_get(hooks, {7: []}))

    assert not verdict.failing
    assert "no deliveries" in verdict.line()


def test_the_window_is_bounded_so_an_old_fault_cannot_mask_a_recovery():
    audit = _module()
    hooks = [{"id": 1, "config": {"url": "u"}, "updated_at": ""}]
    recent_ok = [{"status_code": 200}] * 2
    ancient_failures = [{"status_code": 401}] * 200
    deliveries = {1: recent_ok + ancient_failures}

    [verdict] = audit.verdicts("o/r", "t", get=_fake_get(hooks, deliveries))

    assert verdict.attempted == audit.DELIVERY_WINDOW
    assert not verdict.failing


def test_without_a_token_the_audit_says_it_could_not_look(monkeypatch, capsys):
    """Reporting green because it could not check is the failure mode this replaces."""
    audit = _module()
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    assert audit.main([]) == 1
    assert "no GITHUB_TOKEN" in capsys.readouterr().err


def test_main_exits_non_zero_only_when_a_hook_lands_nothing(monkeypatch, capsys):
    audit = _module()
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    hooks = [{"id": 1, "config": {"url": "u"}, "updated_at": ""}]

    monkeypatch.setattr(
        audit, "_get", _fake_get(hooks, {1: [{"status_code": 200}]}), raising=True
    )
    assert audit.main(["--repo", "o/r"]) == 0

    monkeypatch.setattr(
        audit, "_get", _fake_get(hooks, {1: [{"status_code": 401}]}), raising=True
    )
    assert audit.main(["--repo", "o/r"]) == 1
    assert "1 attempting and landing nothing" in capsys.readouterr().out
