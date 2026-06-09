"""Tests for the watchdog signal ownership consistency audit."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
AUDIT = ROOT / "tools/watchdog_consistency_audit.py"
INVENTORY = ROOT / "docs/ssot/watchdog-signals.yaml"


def _load_audit():
    spec = importlib.util.spec_from_file_location("watchdog_consistency_audit", AUDIT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["watchdog_consistency_audit"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_watchdog_signal_inventory_has_required_ownership_fields() -> None:
    """#209: each monitored signal has a stable owner contract."""
    inventory = yaml.safe_load(INVENTORY.read_text(encoding="utf-8"))
    signals = inventory["signals"]

    assert inventory["version"] == 2
    assert {"internal", "cloudflare", "github", "excluded"} == set(inventory["owners"])
    assert len({signal["signal_id"] for signal in signals}) == len(signals)

    for signal in signals:
        assert signal["environment"]
        assert signal["component"]
        assert signal["signal"]
        assert signal["primary_owner"] in inventory["owners"]


def test_watchdog_consistency_audit_passes_current_contract() -> None:
    """#209: code-owned watchdog configs must match the signal inventory."""
    audit = _load_audit()

    assert audit.audit() == []


def test_watchdog_consistency_audit_rejects_undocumented_exclusions(
    monkeypatch,
) -> None:
    """#209: exclusions must include a reason and revalidation condition."""
    audit = _load_audit()
    original = audit._load_inventory

    def fake_inventory():
        inventory = original()
        copied = {
            **inventory,
            "signals": [dict(signal) for signal in inventory["signals"]],
        }
        for signal in copied["signals"]:
            if signal["signal_id"] == "staging.dokploy.public-route":
                signal.pop("exclusion_reason", None)
        return copied

    monkeypatch.setattr(audit, "_load_inventory", fake_inventory)

    assert any("excluded signal lacks reason" in error for error in audit.audit())


def test_watchdog_consistency_audit_rejects_empty_worker_targets(monkeypatch) -> None:
    """#209: bad JSON overrides must not silently remove every Worker target."""
    audit = _load_audit()
    monkeypatch.setattr(audit, "_worker_keys", lambda: (set(), {("production", "x")}))

    assert any(
        "effective Cloudflare Worker target list is empty" in error
        for error in audit.audit()
    )
