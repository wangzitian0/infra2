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
    assert {"internal", "cloudflare", "github", "excluded", "self"} == set(
        inventory["owners"]
    )
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
    monkeypatch.setattr(
        audit,
        "_worker_keys",
        lambda: (set(), {("production", "x")}, {}),
    )

    assert any(
        "effective Cloudflare Worker target list is empty" in error
        for error in audit.audit()
    )


# --- #425 T5: {tier, type} declaration + mandatory alert debounce -----------

_BASE_SIGNAL = {
    "signal_id": "production.docker.example-check",
    "environment": "production",
    "component": "docker",
    "signal": "example-check",
    "primary_owner": "self",
    "severity": "critical",
}


def test_signal_without_tier_or_type_is_not_required_to_declare_either() -> None:
    """#425 T2 (classify/backfill every entry) is explicitly out of scope for T5:
    a signal that declares neither `tier` nor `type` is left alone."""
    audit = _load_audit()

    assert audit._validate_tier_and_type(dict(_BASE_SIGNAL), "x") == []


def test_signal_with_only_tier_or_only_type_is_rejected() -> None:
    """Declaring one half of `{tier, type}` without the other is not allowed."""
    audit = _load_audit()

    tier_only = {**_BASE_SIGNAL, "tier": "minute"}
    errors = audit._validate_tier_and_type(tier_only, "x")
    assert any("must be declared together" in error for error in errors)

    type_only = {**_BASE_SIGNAL, "type": "alert"}
    errors = audit._validate_tier_and_type(type_only, "x")
    assert any("must be declared together" in error for error in errors)


def test_alert_type_missing_debounce_fields_fails_with_clear_message() -> None:
    """The core T5 guardrail: `type: alert` without a structured debounce fails,
    with a message that names the missing field and explains why (#475/#531)."""
    audit = _load_audit()

    signal = {**_BASE_SIGNAL, "tier": "minute", "type": "alert"}
    errors = audit._validate_tier_and_type(signal, "x")

    assert any(
        "consecutive_failures" in error and "#475" in error and "#531" in error
        for error in errors
    )
    assert any(
        "renotify_window_sec" in error and "#475" in error and "#531" in error
        for error in errors
    )


def test_alert_type_with_non_int_or_negative_debounce_fields_fails() -> None:
    """A free-text threshold or a negative window doesn't satisfy the structured
    requirement: consecutive_failures must be an int >= 1, renotify_window_sec an
    int >= 0."""
    audit = _load_audit()

    signal = {
        **_BASE_SIGNAL,
        "tier": "minute",
        "type": "alert",
        "consecutive_failures": "3 consecutive failures",
        "renotify_window_sec": -1,
    }
    errors = audit._validate_tier_and_type(signal, "x")

    assert any("consecutive_failures" in error for error in errors)
    assert any("renotify_window_sec" in error for error in errors)

    zero_failures = {**signal, "consecutive_failures": 0, "renotify_window_sec": 0}
    errors = audit._validate_tier_and_type(zero_failures, "x")
    assert any("consecutive_failures" in error for error in errors)


def test_zero_renotify_window_is_a_declaration_not_an_absence() -> None:
    """#475 / #903: `renotify_window_sec: 0` declares "never re-page an unchanged
    incident on a timer" — what the breakdown watcher and the probe runner actually
    do. Requiring >= 1 forced the registry to state a timer neither runs."""
    audit = _load_audit()

    signal = {
        **_BASE_SIGNAL,
        "tier": "minute",
        "type": "alert",
        "consecutive_failures": 3,
        "renotify_window_sec": 0,
    }

    assert audit._validate_tier_and_type(signal, "x") == []


def test_alert_type_with_valid_debounce_fields_passes() -> None:
    audit = _load_audit()

    signal = {
        **_BASE_SIGNAL,
        "tier": "minute",
        "type": "alert",
        "consecutive_failures": 3,
        "renotify_window_sec": 1800,
    }

    assert audit._validate_tier_and_type(signal, "x") == []


def test_report_type_does_not_require_debounce_fields() -> None:
    """A `type: report` entry never needs consecutive_failures/renotify_window_sec
    -- reports are time-driven, not event-driven, so there is nothing to debounce."""
    audit = _load_audit()

    signal = {**_BASE_SIGNAL, "tier": "day", "type": "report"}

    assert audit._validate_tier_and_type(signal, "x") == []


def test_cross_cutting_invariant_rejects_hour_report_and_day_alert() -> None:
    """#425 §2: the alert/report boundary IS the day line -- tier<=hour must be
    alert, tier>=day must be report. Mismatches are rejected either direction."""
    audit = _load_audit()

    hour_report = {**_BASE_SIGNAL, "tier": "hour", "type": "report"}
    errors = audit._validate_tier_and_type(hour_report, "x")
    assert any("type=report requires tier in" in error for error in errors)

    day_alert = {
        **_BASE_SIGNAL,
        "tier": "day",
        "type": "alert",
        "consecutive_failures": 3,
        "renotify_window_sec": 1800,
    }
    errors = audit._validate_tier_and_type(day_alert, "x")
    assert any("type=alert requires tier in" in error for error in errors)


def test_invalid_tier_and_type_values_are_rejected() -> None:
    audit = _load_audit()

    signal = {**_BASE_SIGNAL, "tier": "fortnight", "type": "page"}
    errors = audit._validate_tier_and_type(signal, "x")

    assert any("invalid tier" in error for error in errors)
    assert any("invalid type" in error for error in errors)


def test_container_breakdown_watch_registered_and_matches_code() -> None:
    """#531-style drift closure: the registered thresholds must match the live
    defaults in libs/observability/watchers/breakdown_watch.py (the watcher plugin inside
    the #543 single resident sidecar), not just look plausible."""
    import libs.observability.watchers.breakdown_watch as module

    inventory = yaml.safe_load(INVENTORY.read_text(encoding="utf-8"))
    signals = {s["signal"]: s for s in inventory["signals"]}
    entry = signals["container-breakdown-watch"]

    assert entry["tier"] == "minute"
    assert entry["type"] == "alert"
    assert entry["primary_owner"] == "self"
    assert entry["consecutive_failures"] == module.DEFAULT_FAILURE_THRESHOLD
    assert entry["renotify_window_sec"] == module.DEFAULT_RENOTIFY
    assert entry["recovery_threshold"] == module.DEFAULT_RECOVERY_THRESHOLD

    audit = _load_audit()
    assert audit._validate_tier_and_type(entry, entry["signal_id"]) == []


def test_deploy_queue_guard_registered_and_matches_code() -> None:
    """#543: the deploy-queue guard's T5 registration (the #542 exemption come
    due) must match the live defaults in libs/deploy_queue_guard.py. Its
    consecutive_failures=1 is ceiling-qualified: a deploy only counts once it
    has been running past DEPLOY_GUARD_CEILING_SECONDS, so the 1800s ceiling is
    the real debounce and one qualifying sweep legitimately fires."""
    import libs.deploy_queue_guard as module

    inventory = yaml.safe_load(INVENTORY.read_text(encoding="utf-8"))
    signals = {s["signal"]: s for s in inventory["signals"]}
    entry = signals["deploy-queue-guard"]

    assert entry["tier"] == "minute"
    assert entry["type"] == "alert"
    assert entry["primary_owner"] == "self"
    assert entry["consecutive_failures"] == 1
    assert entry["renotify_window_sec"] == module.DEFAULT_RENOTIFY
    assert entry["cadence"] == f"{module.DEFAULT_INTERVAL}s"
    assert str(module.DEFAULT_CEILING) in entry["alert_threshold"]

    audit = _load_audit()
    assert audit._validate_tier_and_type(entry, entry["signal_id"]) == []


def test_vault_self_refresh_audit_registered_as_day_report() -> None:
    """The daily audit pages Feishu only on a confirmed fail (never on a bare
    schedule tick), and is silent on success -- the same shape this SSOT
    already classifies deploy_v2_canary under. tier=day/type=report, not alert:
    it has no consecutive-poll state to debounce (each scheduled run is an
    independent one-shot audit)."""
    inventory = yaml.safe_load(INVENTORY.read_text(encoding="utf-8"))
    signals = {s["signal"]: s for s in inventory["signals"]}
    entry = signals["vault-self-refresh-audit"]

    assert entry["tier"] == "day"
    assert entry["type"] == "report"
    assert entry["primary_owner"] == "self"
    assert "consecutive_failures" not in entry
    assert "renotify_window_sec" not in entry

    audit = _load_audit()
    assert audit._validate_tier_and_type(entry, entry["signal_id"]) == []


# --- #902: one pager per failure class ----------------------------------------


def _layering_errors(audit, edit) -> list[str]:
    """Run only the layering rule over an edited copy of the real inventory."""
    inventory = audit._load_inventory()
    copied = {
        **inventory,
        "signals": [dict(signal) for signal in inventory["signals"]],
        "relayering_debt": [dict(entry) for entry in inventory["relayering_debt"]],
    }
    edit(copied, {signal["signal_id"]: signal for signal in copied["signals"]})
    return audit._validate_layers(copied, copied["signals"])


def test_layering_holds_on_the_current_registry() -> None:
    audit = _load_audit()

    assert _layering_errors(audit, lambda inventory, signals: None) == []


def test_layering_rejects_a_second_pager_for_a_class() -> None:
    audit = _load_audit()

    def github_pages_service_health(inventory, signals):
        signals["global.truealpha-scheduler-liveness.github"]["failure_class"] = (
            "service-health"
        )

    errors = _layering_errors(audit, github_pages_service_health)

    assert errors == [
        "global.truealpha-scheduler-liveness.github: pages service-health from the "
        "github layer, but service-health is paged only by vps (one pager per "
        "failure class)"
    ]


def test_layering_rejects_debt_that_is_already_paid() -> None:
    """A debt entry for a compliant signal would exempt its next regression."""
    audit = _load_audit()

    def stale_debt(inventory, signals):
        inventory["relayering_debt"].append(
            {
                "signal_id": "global.cloudflare-worker-status.github",
                "phase": "#903",
                "reason": "already compliant",
            }
        )

    assert _layering_errors(audit, stale_debt) == [
        "relayering_debt global.cloudflare-worker-status.github is no longer a "
        "violation; remove the entry"
    ]


def test_layering_debt_names_the_phase_that_pays_it() -> None:
    audit = _load_audit()

    def no_phase(inventory, signals):
        inventory["relayering_debt"][0].pop("phase")

    errors = _layering_errors(audit, no_phase)

    assert len(errors) == 1
    assert errors[0].endswith("needs a phase issue (#N) and a reason")


def test_layering_lets_a_report_live_in_any_layer() -> None:
    """Turning a misplaced pager into a report is how #903 pays its debt."""
    audit = _load_audit()

    def ssh_becomes_a_report(inventory, signals):
        signals["global.infra2-ssh.github"].update(type="report", tier="day")
        inventory["relayering_debt"] = [
            entry
            for entry in inventory["relayering_debt"]
            if entry["signal_id"] != "global.infra2-ssh.github"
        ]

    assert _layering_errors(audit, ssh_becomes_a_report) == []


def test_layering_requires_self_signals_to_declare_their_layer() -> None:
    audit = _load_audit()

    def drop_layer(inventory, signals):
        signals["production.docker.container-breakdown-watch"].pop("layer")

    assert _layering_errors(audit, drop_layer) == [
        "production.docker.container-breakdown-watch: self-owned signals declare "
        "layer in ['cloudflare', 'github', 'vps']"
    ]


def test_layering_rejects_an_unclassified_signal() -> None:
    audit = _load_audit()

    def unclassified(inventory, signals):
        signals["production.infra2-backup-production.github"].pop("failure_class")

    assert _layering_errors(audit, unclassified) == [
        "production.infra2-backup-production.github: unknown or missing "
        "failure_class None"
    ]


def test_every_failure_class_has_exactly_one_pager_layer() -> None:
    inventory = yaml.safe_load(INVENTORY.read_text(encoding="utf-8"))

    pagers = {
        name: spec["pager"] for name, spec in inventory["failure_classes"].items()
    }

    assert set(pagers.values()) <= {"vps", "cloudflare", "github"}
    assert pagers["service-health"] == "vps"
    assert pagers["host-reachability"] == pagers["alert-pipeline"] == "cloudflare"


def test_audit_enforces_layering_end_to_end(monkeypatch) -> None:
    """The CI entry point, not just the helper, must fail a second pager."""
    audit = _load_audit()
    original = audit._load_inventory

    def fake_inventory():
        inventory = original()
        inventory["signals"] = [dict(signal) for signal in inventory["signals"]]
        for signal in inventory["signals"]:
            if signal["signal_id"] == "global.cloudflare-worker-status.github":
                signal["failure_class"] = "service-health"
        return inventory

    monkeypatch.setattr(audit, "_load_inventory", fake_inventory)

    assert any(
        error.startswith("global.cloudflare-worker-status.github: pages service-health")
        for error in audit.audit()
    )
