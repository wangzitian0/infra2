"""Equivalence proof for the derived internal watchdog-signal plane (#543).

The handwritten ``primary_owner: internal`` section of watchdog-signals.yaml
(39 entries) was frozen verbatim at
``fixtures/watchdog_internal_signals_frozen.yaml`` the moment it was deleted.
These tests keep :func:`render_internal_signal_entries` field-level equivalent
to that snapshot forever — every intentional normalization is asserted
explicitly below, so a derivation change that silently drops or reclassifies a
signal cannot pass.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

from libs.service_registry import service_attrs, service_id_for_component
from libs.watchdog_signal_entries import render_internal_signal_entries

ROOT = Path(__file__).resolve().parents[2]
FROZEN = Path(__file__).parent / "fixtures/watchdog_internal_signals_frozen.yaml"
AUDIT = ROOT / "tools/watchdog_consistency_audit.py"

# The probe runner's actual shared debounce (tools/infra_probe_runner.py
# DEFAULT_FAILURE_THRESHOLD / DEFAULT_RENOTIFY_SECONDS) — what every derived
# entry must state, because the registry documents what the runner DOES.
RUNNER_CONSECUTIVE_FAILURES = 3
RUNNER_RENOTIFY_WINDOW_SEC = 1800


def _frozen() -> dict[tuple[str, str], dict]:
    signals = yaml.safe_load(FROZEN.read_text(encoding="utf-8"))["signals"]
    assert len(signals) == 39
    return {(s["environment"], s["signal"]): s for s in signals}


def _rendered() -> dict[tuple[str, str], dict]:
    entries = render_internal_signal_entries()
    keyed = {(e["environment"], e["signal"]): e for e in entries}
    assert len(keyed) == len(entries), "duplicate (environment, signal) keys"
    return keyed


def test_derivation_covers_exactly_the_frozen_signal_set() -> None:
    """Zero signals dropped, zero invented: the (environment, signal) sets are
    identical to the handwritten registry at the moment of deletion."""
    assert set(_rendered()) == set(_frozen())


def test_severity_cadence_and_identity_match_frozen_entries() -> None:
    """Per-entry: same severity, same cadence, and the frozen component alias
    resolves to the same service_id the derived entry carries directly."""
    frozen = _frozen()
    for key, entry in _rendered().items():
        old = frozen[key]
        assert entry["severity"] == old["severity"], key
        assert entry["cadence"] == old["cadence"], key
        assert entry["primary_owner"] == "internal" == old["primary_owner"], key
        resolved = service_id_for_component(old["component"], signal=old["signal"])
        assert entry["service_id"] == resolved, key
        # the derived component must resolve identically (the audit's check)
        assert (
            service_id_for_component(entry["component"], signal=entry["signal"])
            == resolved
        ), key


def test_expected_field_matches_frozen_machine_readable_values() -> None:
    """`expected` carries over verbatim where the handwritten value was the
    machine value (vault's status-code list). The three host entries held
    free-text summaries ("<=80% CPU") of the same ProbeFacet ceiling — those
    normalize to the machine value "80" the runner actually parses."""
    frozen = _frozen()
    rendered = _rendered()
    for key, old in frozen.items():
        if "expected" not in old:
            continue
        if old["signal"].startswith("host-"):
            assert old["expected"].startswith("<=80%"), key
            assert rendered[key]["expected"] == "80", key
        else:
            assert rendered[key]["expected"] == old["expected"], key


def test_structured_debounce_supersedes_frozen_free_text_threshold() -> None:
    """Every handwritten entry said `alert_threshold: 3 consecutive failures`
    (free text, unenforceable). The derived entries state the same threshold
    in #425 T5's structured fields, matching the runner's real defaults.

    One handwritten entry (production.dokploy.internal-http) claimed
    `renotify_window_sec: 3600` — an aspiration nothing implemented; the
    runner renotifies every 1800s for all probes. The derivation states the
    truth, so that entry normalizes 3600 -> 1800.
    """
    frozen = _frozen()
    for key, entry in _rendered().items():
        old = frozen[key]
        threshold_text = old.get("alert_threshold", "")
        assert threshold_text.startswith(str(RUNNER_CONSECUTIVE_FAILURES)), (
            f"{key}: fixture drifted? {threshold_text!r}"
        )
        assert entry["consecutive_failures"] == RUNNER_CONSECUTIVE_FAILURES, key
        assert entry["renotify_window_sec"] == RUNNER_RENOTIFY_WINDOW_SEC, key
    assert (
        frozen[("production", "dokploy-internal-http")].get("renotify_window_sec")
        == 3600
    )


def test_frozen_enrichment_fields_were_documentation_only() -> None:
    """retry_count / failure_domain_whitelist / expected_sla_pct existed on
    exactly ONE handwritten entry and are consumed by no code (verified by
    repo-wide grep at deletion time), so the derivation drops them. This test
    pins that the drop loses documentation on that one entry and nothing
    else — if the fixture ever says otherwise, the drop needs a rethink."""
    carriers = [
        key
        for key, old in _frozen().items()
        if any(
            f in old
            for f in ("retry_count", "failure_domain_whitelist", "expected_sla_pct")
        )
    ]
    assert carriers == [("production", "dokploy-internal-http")]


def test_every_internal_entry_is_fully_classified() -> None:
    """#425 T5 backfill is COMPLETE for the internal plane: all derived
    entries carry tier=minute / type=alert plus the structured debounce —
    no unclassified stragglers can reappear without failing here."""
    for key, entry in _rendered().items():
        assert entry["tier"] == "minute", key
        assert entry["type"] == "alert", key


def test_render_fails_closed_on_empty_registry_walk() -> None:
    with pytest.raises(ValueError, match="ZERO entries"):
        render_internal_signal_entries(attrs={})


def test_signal_id_stays_unique_against_cross_plane_entries() -> None:
    """Derived ids (f"{env}.{probe-name}") must never collide with the
    handwritten cross-plane ids (f"{env}.{component}.{suffix}")."""
    yaml_ids = {
        s["signal_id"]
        for s in yaml.safe_load(
            (ROOT / "docs/ssot/watchdog-signals.yaml").read_text(encoding="utf-8")
        )["signals"]
    }
    derived_ids = {e["signal_id"] for e in render_internal_signal_entries()}
    assert not yaml_ids & derived_ids


def test_audit_rejects_handwritten_internal_entries(tmp_path, monkeypatch) -> None:
    """The audit fails closed if someone hand-writes an internal entry back
    into the YAML instead of declaring facets on the service's deploy.py."""
    spec = importlib.util.spec_from_file_location("watchdog_consistency_audit", AUDIT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["watchdog_consistency_audit"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)

    stray = tmp_path / "watchdog-signals.yaml"
    stray.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "owners": ["internal"],
                "signals": [
                    {
                        "signal_id": "production.sneaky.internal-http",
                        "environment": "production",
                        "component": "signoz",
                        "signal": "sneaky-http",
                        "primary_owner": "internal",
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(module, "INVENTORY", stray)
    with pytest.raises(ValueError, match="handwritten"):
        module._load_inventory()


def test_probe_declaring_services_all_carry_a_signal_facet() -> None:
    """The Infra-012.10 gap is closed and stays closed: every registry service
    that declares ProbeFacets also declares its SignalFacet classification."""
    for service_id, meta in service_attrs().items():
        if meta.probes:
            assert meta.signals, (
                f"{service_id} declares probes but no SignalFacet — the "
                f"derived watchdog-signals entries would be unclassified "
                f"(Infra-012.10 / #425 T5)"
            )
