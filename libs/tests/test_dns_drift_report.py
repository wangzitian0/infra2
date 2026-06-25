"""Tests for the DNS drift reconciler's pure core (compute_drift + format_report).

The live parts (Cloudflare read, Lark post) need secrets and run only in the scheduled
workflow; the diff + report formatting — where the actual logic lives — are tested here offline.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _mod():
    spec = importlib.util.spec_from_file_location(
        "dns_drift_report", ROOT / "tools/dns_drift_report.py"
    )
    module = importlib.util.module_from_spec(spec)
    # Register before exec: @dataclass + `from __future__ import annotations` resolves its
    # string annotations via sys.modules[__module__], which fails if the module isn't there.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_compute_drift_splits_missing_and_unmanaged() -> None:
    m = _mod()
    expected = {"sso.zitian.party", "vault.zitian.party", "op.zitian.party"}
    actual = {"sso.zitian.party", "vault.zitian.party", "stray.zitian.party"}
    drift = m.compute_drift(expected, actual)

    assert drift.missing == ["op.zitian.party"]  # intended, absent in CF -> real drift
    assert drift.unmanaged == ["stray.zitian.party"]  # in CF, not intended -> info
    assert drift.in_sync is False  # missing is non-empty


def test_compute_drift_in_sync_ignores_unmanaged_for_status() -> None:
    m = _mod()
    drift = m.compute_drift({"a.x"}, {"a.x", "extra.x"})
    assert (
        drift.in_sync is True
    )  # every intended record exists; extras don't break sync
    assert drift.unmanaged == ["extra.x"]


def test_format_report_flags_missing_and_confirms_in_sync() -> None:
    m = _mod()
    missing = m.format_report(m.compute_drift({"op.x"}, set()), "x", 1, 0)
    assert "🔴 MISSING" in missing and "op.x" in missing

    ok = m.format_report(m.compute_drift({"a.x"}, {"a.x"}), "x", 1, 1)
    assert "✅ in sync" in ok


def test_delivery_mode_prefers_app_then_webhook(monkeypatch) -> None:
    m = _mod()
    for k in (
        "DNS_DRIFT_FEISHU_APP_ID",
        "DNS_DRIFT_FEISHU_APP_SECRET",
        "DNS_DRIFT_FEISHU_CHAT_ID",
        "DNS_DRIFT_FEISHU_WEBHOOK_URL",
    ):
        monkeypatch.delenv(k, raising=False)
    assert m.delivery_mode() is None  # nothing configured -> no-op

    monkeypatch.setenv("DNS_DRIFT_FEISHU_WEBHOOK_URL", "https://hook")
    assert m.delivery_mode() == "webhook"

    monkeypatch.setenv("DNS_DRIFT_FEISHU_APP_ID", "a")
    monkeypatch.setenv("DNS_DRIFT_FEISHU_APP_SECRET", "s")
    monkeypatch.setenv("DNS_DRIFT_FEISHU_CHAT_ID", "oc_x")
    assert m.delivery_mode() == "app"  # app bot preferred over webhook


def test_expected_records_strips_wrapping_quotes(monkeypatch) -> None:
    """CF_RECORDS arrives quoted from op; the wrapping quotes must be stripped, else the
    first/last name keeps a stray quote and falsely reports MISSING (the live-drill bug)."""
    m = _mod()

    class _DummyDns:
        DEFAULT_RECORDS = ("cloud", "op")

        @staticmethod
        def _normalize_record(name: str, domain: str) -> str:
            return f"{name}.{domain}"

    monkeypatch.setenv("INTERNAL_DOMAIN", "x")
    monkeypatch.setenv("CF_RECORDS", '"cloud,op"')
    assert m._expected_records(_DummyDns) == ["cloud.x", "op.x"]  # no stray quote
