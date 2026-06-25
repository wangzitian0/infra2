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
    assert drift.in_sync is True  # every intended record exists; extras don't break sync
    assert drift.unmanaged == ["extra.x"]


def test_format_report_flags_missing_and_confirms_in_sync() -> None:
    m = _mod()
    missing = m.format_report(m.compute_drift({"op.x"}, set()), "x", 1, 0)
    assert "🔴 MISSING" in missing and "op.x" in missing

    ok = m.format_report(m.compute_drift({"a.x"}, {"a.x"}), "x", 1, 1)
    assert "✅ in sync" in ok
