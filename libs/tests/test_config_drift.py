"""Tests for the config-drift reconciler's pure pieces + the path-independence guarantee.

The live parts (Dokploy reads, git cat-file at a tag, Feishu post) run only in the scheduled
workflow; the hash purity, report formatting, and delivery no-op — where the logic lives — are
tested offline here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from libs.deploy.deployer import config_hash_from_items

ROOT = Path(__file__).resolve().parents[2]


def test_config_hash_is_pure_and_path_independent() -> None:
    """Same (compose, env, repo-relative items) -> same hash, with NO dependence on cwd or
    absolute paths (labels are repo-relative strings). This is what lets the reconciler recompute
    a service's hash from a git ref and match what the deploy computed on disk."""
    compose = "services: {a: {image: x}}"
    env = {"ENV": "production", "INTERNAL_DOMAIN": "zitian.party"}
    artifact = [("libs/a.py", b"print(1)"), ("tools/b.py", b"x=2")]
    deps = [("libs/dep.py", b"shared")]

    h1 = config_hash_from_items(compose, env, artifact, deps)
    h2 = config_hash_from_items(compose, env, artifact, deps)
    assert h1 == h2  # deterministic
    assert len(h1) == 12  # sha256[:12]

    # content change flips the hash
    assert (
        config_hash_from_items(compose, env, [("libs/a.py", b"print(2)")], deps) != h1
    )
    # env change flips the hash
    assert (
        config_hash_from_items(compose, {**env, "ENV": "staging"}, artifact, deps) != h1
    )
    # a dependency change flips the hash (the cross-service fold)
    assert (
        config_hash_from_items(compose, env, artifact, [("libs/dep.py", b"changed")])
        != h1
    )


def _drift_mod():
    spec = importlib.util.spec_from_file_location(
        "dokploy_config_drift", ROOT / "tools/dokploy_config_drift.py"
    )
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_format_report_flags_drift_and_confirms_in_sync() -> None:
    m = _drift_mod()
    Row = m.Row
    drift = m.format_report("v1.2.3", [Row("platform/x", "DRIFT", "aaa", "bbb")])
    assert "🔴 DRIFT platform/x" in drift and "v1.2.3" in drift

    ok = m.format_report("v1.2.3", [Row("platform/y", "in_sync", "aaa", "aaa")])
    assert "✅ every comparable service matches release v1.2.3" in ok


def test_format_report_surfaces_errors_and_does_not_call_them_drift() -> None:
    """A compute failure must be surfaced (not silently skipped) AND must not be counted as
    drift — else a run looks healthy while it never actually checked the service."""
    m = _drift_mod()
    Row = m.Row
    out = m.format_report(
        "v1.2.3", [Row("platform/z", "error", note="compute error: boom")]
    )
    assert "⚠️ ERROR platform/z" in out  # surfaced loudly
    assert "DRIFT 0" in out  # an error is NOT drift
    assert "🔴 DRIFT" not in out


def test_deliver_infra2_report_noops_when_unconfigured(monkeypatch) -> None:
    from libs.alerting import deliver_infra2_report

    for k in (
        "INFRA2_REPORTS_FEISHU_APP_ID",
        "INFRA2_REPORTS_FEISHU_APP_SECRET",
        "INFRA2_REPORTS_FEISHU_CHAT_ID",
    ):
        monkeypatch.delenv(k, raising=False)
    assert (
        deliver_infra2_report("hi") is False
    )  # not configured -> clean no-op, no raise
