"""Tests for the DNS drift reconciler's pure core (compute_drift + format_report).

The live parts (Cloudflare read, Lark post) need secrets and run only in the scheduled
workflow; the diff + report formatting — where the actual logic lives — are tested here offline.
"""

from __future__ import annotations

from common.deploy import dns_drift as m


def _mod():
    return m


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


def test_actual_records_never_turns_observation_failure_into_empty_zone(
    monkeypatch,
) -> None:
    """A Cloudflare 401/transport failure is unknown reality, never zero records."""
    m = _mod()

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class _DummyDns:
        @staticmethod
        def _cloudflare_client(_token):
            return _Client()

        @staticmethod
        def _resolve_zone_id(_client, _zone_id, _zone_name):
            return "zone-1"

        @staticmethod
        def _cf_request(_client, _method, _path, **_kwargs):
            return None

    monkeypatch.setenv("CF_API_TOKEN", "expired")
    monkeypatch.setenv("CF_ZONE_ID", "zone-1")

    import pytest

    with pytest.raises(RuntimeError, match="observation failed"):
        m._actual_records(_DummyDns)


def test_dns_drift_report_goes_out_as_a_card_in_the_card_standard(monkeypatch) -> None:
    """#1648: the report reaches the reports chat as a card (the only delivery left)."""
    import common.alerting as alerting
    from common.tests.test_alerting import _assert_card_standard

    sent: list[dict] = []
    monkeypatch.setattr(
        alerting, "deliver_feishu_app_card", lambda **kw: sent.append(kw["card"])
    )
    env = {
        "INFRA2_REPORTS_FEISHU_APP_ID": "a",
        "INFRA2_REPORTS_FEISHU_APP_SECRET": "b",
        "INFRA2_REPORTS_FEISHU_CHAT_ID": "oc_reports",
    }
    drift = m.compute_drift({"op.x", "a.x"}, {"a.x", "stray.x"})
    report = m.format_report(drift, "zitian.party", 2, 2)

    assert alerting.deliver_infra2_report(report, env)

    (card,) = sent
    _assert_card_standard(card)
    assert "MISSING in Cloudflare" in card["elements"][0]["text"]["content"]
    assert not hasattr(m, "deliver") and not hasattr(m, "main")  # the text path is gone
