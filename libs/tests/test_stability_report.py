"""Tests for the stability-report CLI runner (aggregation lives in the lib)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "tools" / "stability_report.py"

LEDGER = {
    "as_of": "2026-06-10",
    "window_days": 1,
    "ledger": [
        {
            "date": "2026-06-10",
            "runs": 48,
            "signals": {
                "production:minio-public-route": {
                    "ok": 46,
                    "fail": 2,
                    "lastDomain": "network",
                }
            },
        }
    ],
}


def _load_module():
    spec = importlib.util.spec_from_file_location("stability_report", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("Failed to load stability_report module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---- 正例: dry-run renders a report without delivering ----------------------


def test_positive_dry_run_prints_report_without_delivery(tmp_path, capsys) -> None:
    sync = _load_module()
    ledger_file = tmp_path / "ledger.json"
    ledger_file.write_text(json.dumps(LEDGER))

    rc = sync.run({"INFRA2_STABILITY_REPORT_DRY_RUN": "1"}, input_path=str(ledger_file))

    assert rc == 0
    out = capsys.readouterr().out
    assert "[STABILITY]" in out
    assert "minio-public-route" in out  # degraded signal surfaced, not hidden


# ---- 正例: missing config falls back to the public Worker default, never a
# silent no-op (finance_report#1851 G4) ---------------------------------------


def test_positive_missing_env_falls_back_to_default_ledger_url(
    monkeypatch, capsys
) -> None:
    """No INFRA2_WATCHDOG_LEDGER_URL configured must still fetch and report —
    mirrors out_of_band_watchdog.DEFAULT_WORKER_STATUS_URL's existing pattern,
    not the old fail-closed shape that let the weekly digest silently skip this
    step for three consecutive weeks."""
    sync = _load_module()
    captured: dict[str, str] = {}

    def fake_fetch(url: str, token: str, *, timeout: float = 20.0) -> dict:
        captured["url"] = url
        return LEDGER

    monkeypatch.setattr(sync, "fetch_ledger", fake_fetch)

    rc = sync.run({"INFRA2_STABILITY_REPORT_DRY_RUN": "1"}, input_path=None)

    assert rc == 0
    assert captured["url"] == sync.DEFAULT_LEDGER_URL
    assert "[STABILITY]" in capsys.readouterr().out


# ---- 反例: misconfiguration fails loudly, no silent success -----------------


def test_negative_empty_default_still_fails_loudly(monkeypatch, capsys) -> None:
    """Defensive: if DEFAULT_LEDGER_URL were ever cleared, the script must still
    fail loudly (rc=2 + stderr) rather than silently produce an empty report."""
    sync = _load_module()
    monkeypatch.setattr(sync, "DEFAULT_LEDGER_URL", "")

    rc = sync.run({}, input_path=None)  # no --input, no env var, no default

    assert rc == 2
    assert "required" in capsys.readouterr().err
