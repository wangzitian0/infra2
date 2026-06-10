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
            "signals": {"production:minio-public-route": {"ok": 46, "fail": 2, "lastDomain": "network"}},
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


# ---- 反例: misconfiguration fails loudly, no silent success -----------------


def test_negative_missing_ledger_source_returns_error(capsys) -> None:
    sync = _load_module()
    rc = sync.run({}, input_path=None)  # no --input and no INFRA2_WATCHDOG_LEDGER_URL
    assert rc == 2
    assert "required" in capsys.readouterr().err
