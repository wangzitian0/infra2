"""Derived internal watchdog-signal plane (#543): classification, debounce and fail-closed rules."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from common.core.registry import service_attrs, service_id_for_component
from common.observability.signal_entries import render_internal_signal_entries
from common.tests.conftest import load_repo_module

ROOT = Path(__file__).resolve().parents[2]
AUDIT = ROOT / "common/observability/watchdog_audit.py"


def _probe_runner():
    return load_repo_module(
        "common/observability/infra_probe_runner.py",
        "infra_probe_runner_for_signal_entries",
    )


# The probe runner's actual shared debounce (common/observability/infra_probe_runner.py
# DEFAULT_FAILURE_THRESHOLD / DEFAULT_RENOTIFY_SECONDS) — what every derived
# entry must state, because the registry documents what the runner DOES. Read
# from the runner itself (#903), so a SignalFacet that restates a stale value
# fails here instead of drifting.
RUNNER_CONSECUTIVE_FAILURES = _probe_runner().DEFAULT_FAILURE_THRESHOLD
RUNNER_RENOTIFY_WINDOW_SEC = _probe_runner().DEFAULT_RENOTIFY_SECONDS


def _rendered() -> dict[tuple[str, str], dict]:
    entries = render_internal_signal_entries()
    keyed = {(e["environment"], e["signal"]): e for e in entries}
    assert len(keyed) == len(entries), "duplicate (environment, signal) keys"
    return keyed


def test_every_derived_entry_states_the_runner_debounce_and_resolves_its_service() -> (
    None
):
    """Each entry carries the runner's real debounce and a component that resolves to its service_id."""
    rendered = _rendered()
    assert rendered, "derivation produced zero entries"
    for key, entry in rendered.items():
        assert entry["primary_owner"] == "internal", key
        assert entry["consecutive_failures"] == RUNNER_CONSECUTIVE_FAILURES, key
        assert entry["renotify_window_sec"] == RUNNER_RENOTIFY_WINDOW_SEC, key
        assert (
            service_id_for_component(entry["component"], signal=entry["signal"])
            == entry["service_id"]
        ), key


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
    from common.observability import watchdog_audit as module

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


def test_an_alert_facet_must_declare_its_renotify_window() -> None:
    """#903 review: `renotify_window_sec=0` is a real declaration ("never on a
    timer"), so a default of 0 made a forgotten window indistinguishable from a
    declared one. Left out, it stays None and the derived entry fails the audit;
    declared as 0 it passes."""
    from common.core.facets import ProbeFacet, SignalFacet
    from common.core.registry import ServiceMeta
    from common.observability import watchdog_audit as audit

    def derived_errors(signal: SignalFacet) -> list[str]:
        meta = ServiceMeta(
            service_id="platform/example",
            layer="platform",
            service="example",
            prod_only=False,
            subdomain=None,
            service_port=None,
            service_name=None,
            telemetry_service_name=None,
            telemetry_component=None,
            project="platform",
            probes=(ProbeFacet(name="example-http", kind="http", target="http://x"),),
            signals=(signal,),
        )
        [entry, *_] = render_internal_signal_entries(attrs={"platform/example": meta})
        return audit._validate_tier_and_type(entry, entry["signal_id"])

    forgotten = SignalFacet(tier="minute", type="alert", consecutive_failures=3)
    assert forgotten.renotify_window_sec is None
    assert any("renotify_window_sec" in error for error in derived_errors(forgotten))
    declared = SignalFacet(
        tier="minute", type="alert", consecutive_failures=3, renotify_window_sec=0
    )
    assert derived_errors(declared) == []
