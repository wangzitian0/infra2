"""Tests for tools/facet_reconcile.py (#542 task 4) — pure wiring, no live calls."""

from __future__ import annotations

from tools import facet_reconcile as fr


def _section(**kw):
    base = dict(name="x", report="r", blockers=[], confirmed=[], skipped="")
    base.update(kw)
    return fr.Section(**base)


def test_combined_report_contains_every_section_and_counts():
    sections = [
        _section(name="compose-id", report="A ok"),
        _section(name="dns", report="B drift", blockers=["b1"], confirmed=["b1"]),
    ]
    text = fr.combined_report(sections)
    assert "compose-id" in text and "dns" in text
    assert "blockers 1" in text and "confirmed 1" in text
    assert "A ok" in text and "B drift" in text


def test_confirmed_findings_are_section_tagged():
    sections = [_section(name="dns", confirmed=["rec X missing"])]
    assert fr.confirmed_findings(sections) == ["[dns] rec X missing"]


def test_transient_blocker_fails_job_but_never_pages():
    # the #524 discipline, preserved across absorption: a lookup error is a
    # blocker (job goes red, retried next schedule) but NOT a confirmed finding
    sections = [_section(name="compose-id", blockers=["error"], confirmed=[])]
    assert any(s.blockers for s in sections)
    assert fr.confirmed_findings(sections) == []


def test_dns_section_skips_cleanly_when_unconfigured(monkeypatch):
    for var in ("CF_API_TOKEN", "CF_ZONE_ID", "CF_ZONE_NAME", "INTERNAL_DOMAIN"):
        monkeypatch.delenv(var, raising=False)
    section = fr.run_dns_section()
    assert section.skipped
    assert not section.blockers  # not-yet-configured is never a red


def test_main_delivers_report_even_when_clean(monkeypatch, capsys):
    monkeypatch.setattr(fr, "run_all", lambda: [_section(name="compose-id")])
    import libs.alerting as alerting

    delivered = {}
    monkeypatch.setattr(
        alerting, "deliver_infra2_report", lambda text: delivered.setdefault("t", text) or True
    )
    assert fr.main() == 0
    assert "facet reconcile" in delivered["t"]  # self-proving delivery (#425)


def test_main_exits_nonzero_on_any_blocker(monkeypatch):
    monkeypatch.setattr(
        fr, "run_all", lambda: [_section(name="dns", blockers=["x"], confirmed=["x"])]
    )
    import libs.alerting as alerting

    monkeypatch.setattr(alerting, "deliver_infra2_report", lambda text: True)
    assert fr.main() == 1
