#!/usr/bin/env python3
"""Audit watchdog signal ownership against live code-owned configs.

Also enforces (#425 T5) the optional-but-if-present-mandatory `{tier, type}`
declaration on each signal, and, for `type: alert` signals, a structured
debounce (`consecutive_failures` + `renotify_window_sec`) -- see
_validate_tier_and_type() and the YAML inventory's header comment for the
full rule and rationale.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tomllib
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.service_registry import service_id_for_component  # noqa: E402

INVENTORY = ROOT / "docs/ssot/watchdog-signals.yaml"
WRANGLER = ROOT / "cloudflare/infra-watchdog/wrangler.toml"
OUT_OF_BAND = ROOT / "tools/out_of_band_watchdog.py"

VALID_OWNERS = {"internal", "cloudflare", "github", "excluded", "self"}

# #425 T5: the four cadence tiers + two signal types from ops.observability.md §2
# (the SSOT law landed by #425 T1/#426). `tier`/`type` are OPTIONAL on a signal
# (backfilling every existing entry is #425 T2's classify job, not this gate's) --
# but once a signal declares either, both are required and must be internally
# consistent, and declaring `type: alert` pulls in the mandatory debounce fields
# below. See _validate_inventory() and the YAML header comment for the full rule.
VALID_TIERS = {"minute", "hour", "day", "month"}
VALID_TYPES = {"alert", "report"}
# Cross-cutting invariant (ops.observability.md §2): tiers <= hour are event-driven
# ALERTs, tiers >= day are time-driven REPORTs. The alert/report boundary IS the
# day line -- there is no such thing as an hourly report or a monthly alert.
ALERT_TIERS = {"minute", "hour"}
REPORT_TIERS = {"day", "month"}


def main() -> int:
    errors = audit()
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("watchdog consistency audit passed")
    return 0


def audit() -> list[str]:
    inventory = _load_inventory()
    signals = inventory.get("signals", [])
    errors: list[str] = []
    errors.extend(_validate_inventory(signals))

    internal_specs = _compose_probe_specs()
    worker_targets, worker_heartbeats, worker_identities = _worker_keys()
    github_signals = _github_signal_names()

    expected_internal = _signals_by_owner(signals, "internal")
    expected_cloudflare = _signals_by_owner(signals, "cloudflare")
    expected_github = _signals_by_owner(signals, "github")
    expected_excluded = _signals_by_owner(signals, "excluded")

    errors.extend(
        _missing(
            "internal probe spec",
            {(signal["environment"], signal["signal"]) for signal in expected_internal},
            {
                (signal["environment"], name)
                for signal in expected_internal
                for name in internal_specs
                if name == signal["signal"]
            },
        )
    )
    expected_internal_keys = {
        (signal["environment"], signal["signal"]) for signal in expected_internal
    }
    configured_internal_keys = {
        (environment, name)
        for name in internal_specs
        for environment in (
            ("production",) if name.startswith("host-") else ("production", "staging")
        )
    }
    for key in sorted(configured_internal_keys - expected_internal_keys):
        errors.append(f"configured internal probe lacks inventory entry: {key}")

    for signal in expected_internal:
        name = signal["signal"]
        if name not in internal_specs:
            continue
        try:
            expected_service_id = service_id_for_component(
                signal["component"], signal=name
            )
        except ValueError as exc:
            errors.append(
                f"cannot resolve service identity for {signal['signal_id']}: {exc}"
            )
            continue
        if internal_specs[name] != expected_service_id:
            errors.append(
                f"internal probe service_id mismatch for {name}: "
                f"expected {expected_service_id}, got {internal_specs[name] or 'missing'}"
            )

    cloudflare_expected_keys = {
        (signal["environment"], signal["signal"])
        for signal in expected_cloudflare
        if signal["signal"].endswith("public-route")
    }
    heartbeat_expected_keys = {
        (signal["environment"], signal["signal"])
        for signal in expected_cloudflare
        if not signal["signal"].endswith("public-route")
    }
    errors.extend(
        _missing("Cloudflare Worker target", cloudflare_expected_keys, worker_targets)
    )
    errors.extend(
        _missing(
            "Cloudflare Worker heartbeat", heartbeat_expected_keys, worker_heartbeats
        )
    )

    configured_cloudflare = worker_targets | worker_heartbeats
    inventory_cloudflare = cloudflare_expected_keys | heartbeat_expected_keys
    for key in sorted(configured_cloudflare - inventory_cloudflare):
        errors.append(f"configured Cloudflare signal lacks inventory entry: {key}")
    for signal in expected_cloudflare:
        key = (signal["environment"], signal["signal"])
        try:
            expected_service_id = service_id_for_component(
                signal["component"], signal=signal["signal"]
            )
        except ValueError:
            continue
        if worker_identities.get(key) != expected_service_id:
            errors.append(
                f"Cloudflare signal service_id mismatch for {key}: expected "
                f"{expected_service_id}, got {worker_identities.get(key) or 'missing'}"
            )

    for signal in expected_github:
        if signal["signal"] not in github_signals:
            errors.append(f"missing GitHub watchdog signal: {signal['signal']}")
    for name in sorted(
        github_signals - {signal["signal"] for signal in expected_github}
    ):
        errors.append(
            f"configured GitHub watchdog signal lacks inventory entry: {name}"
        )

    for signal in expected_excluded:
        key = (signal["environment"], signal["signal"])
        if not signal.get("exclusion_reason") or not signal.get(
            "revalidation_condition"
        ):
            errors.append(f"excluded signal lacks reason or revalidation: {key}")
        if key in configured_cloudflare:
            errors.append(f"excluded signal is still configured in Cloudflare: {key}")

    if not worker_targets:
        errors.append("effective Cloudflare Worker target list is empty")
    if not worker_heartbeats:
        errors.append("effective Cloudflare Worker heartbeat list is empty")

    return errors


def _validate_inventory(signals: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    seen_ids: set[str] = set()
    required = {"signal_id", "environment", "component", "signal", "primary_owner"}
    for signal in signals:
        missing = required - set(signal)
        if missing:
            errors.append(f"signal missing fields {sorted(missing)}: {signal}")
        signal_id = str(signal.get("signal_id", ""))
        if signal_id in seen_ids:
            errors.append(f"duplicate signal_id: {signal_id}")
        seen_ids.add(signal_id)
        owner = signal.get("primary_owner")
        if owner not in VALID_OWNERS:
            errors.append(f"invalid primary_owner for {signal_id}: {owner}")
        try:
            service_id_for_component(
                str(signal.get("component", "")), signal=str(signal.get("signal", ""))
            )
        except ValueError as exc:
            errors.append(f"unresolvable service identity for {signal_id}: {exc}")
        errors.extend(_validate_tier_and_type(signal, signal_id))
    return errors


def _validate_tier_and_type(signal: dict[str, Any], signal_id: str) -> list[str]:
    """#425 T5: enforce the `{tier, type}` declaration and, for alerts, its debounce.

    `tier`/`type` are optional per-signal (see the module-level comment for why),
    so a signal that declares neither is left alone -- that is #425 T2's backlog,
    not a failure here. But once EITHER is declared, both must be present, both
    must be valid, and they must agree with the alert<=hour/report>=day invariant.

    For `type: alert` specifically, this also enforces the debounce/threshold this
    issue's comment thread calls out as the actual point of T5: "an alert-tier
    check must declare not only {tier, type} but, for type=alert, a
    debounce/threshold = what distinguishes a real failure from a transient
    blip." `container_breakdown_watch.py` (#475) and the route-canary's single-shot
    30s-timeout page (also #425) are the two concrete incidents this line is
    closing -- both were exactly a check with no declared debounce paging on a
    transient blip.
    """
    tier = signal.get("tier")
    type_ = signal.get("type")
    if tier is None and type_ is None:
        return []

    errors: list[str] = []
    if tier is None or type_ is None:
        errors.append(
            f"{signal_id}: tier and type must be declared together "
            f"(got tier={tier!r}, type={type_!r})"
        )
        return errors

    tier_valid = tier in VALID_TIERS
    type_valid = type_ in VALID_TYPES
    if not tier_valid:
        errors.append(
            f"{signal_id}: invalid tier {tier!r} (must be one of {sorted(VALID_TIERS)})"
        )
    if not type_valid:
        errors.append(
            f"{signal_id}: invalid type {type_!r} (must be one of {sorted(VALID_TYPES)})"
        )
    if not (tier_valid and type_valid):
        return errors

    if type_ == "alert" and tier not in ALERT_TIERS:
        errors.append(
            f"{signal_id}: type=alert requires tier in {sorted(ALERT_TIERS)} "
            f"(#425 cross-cutting invariant: alert/report boundary is the day "
            f"line), got tier={tier!r}"
        )
    if type_ == "report" and tier not in REPORT_TIERS:
        errors.append(
            f"{signal_id}: type=report requires tier in {sorted(REPORT_TIERS)} "
            f"(#425 cross-cutting invariant: alert/report boundary is the day "
            f"line), got tier={tier!r}"
        )

    if type_ == "alert":
        consecutive_failures = signal.get("consecutive_failures")
        if (
            not isinstance(consecutive_failures, int)
            or isinstance(consecutive_failures, bool)
            or consecutive_failures < 1
        ):
            errors.append(
                f"{signal_id}: type=alert requires an int consecutive_failures >= 1 "
                f"(the debounce that distinguishes a real failure from a transient "
                f"blip -- #425 T5 / #475 / #531), got {consecutive_failures!r}"
            )
        renotify_window_sec = signal.get("renotify_window_sec")
        if (
            not isinstance(renotify_window_sec, int)
            or isinstance(renotify_window_sec, bool)
            or renotify_window_sec < 1
        ):
            errors.append(
                f"{signal_id}: type=alert requires an int renotify_window_sec >= 1 "
                f"seconds (#425 T5 / #475 / #531), got {renotify_window_sec!r}"
            )
    return errors


def _signals_by_owner(
    signals: list[dict[str, Any]], owner: str
) -> list[dict[str, Any]]:
    return [signal for signal in signals if signal.get("primary_owner") == owner]


def _missing(
    label: str, expected: set[tuple[str, str]], actual: set[tuple[str, str]]
) -> list[str]:
    return [f"missing {label}: {key}" for key in sorted(expected - actual)]


def _load_inventory() -> dict[str, Any]:
    """The YAML (cross-plane, handwritten) + the derived internal plane (#543).

    `primary_owner: internal` entries are generated from each service's
    ProbeFacet/SignalFacet declarations, so the internal set-equality and
    identity checks in audit() hold by construction; what this gate still
    buys on the internal plane is _validate_inventory()'s tier/type/debounce
    enforcement over the DERIVED entries — a deploy.py declaring a bad
    SignalFacet fails CI here.
    """
    from libs.watchdog_signal_entries import render_internal_signal_entries

    inventory = yaml.safe_load(INVENTORY.read_text(encoding="utf-8"))
    handwritten = inventory.get("signals", [])
    stray = [
        s["signal_id"] for s in handwritten if s.get("primary_owner") == "internal"
    ]
    if stray:
        raise ValueError(
            f"handwritten `primary_owner: internal` entries in {INVENTORY.name}: "
            f"{stray} — the internal plane is derived from ProbeFacet/SignalFacet "
            f"declarations (#543); declare facets on the service's deploy.py instead"
        )
    inventory["signals"] = handwritten + render_internal_signal_entries()
    return inventory


def _compose_probe_specs() -> dict[str, str]:
    """Probe name -> service_id of the internal probe set.

    #541 cutover: the specs are no longer a compose.yaml literal — they are
    rendered from each service's ProbeFacet declarations via the registry, so
    this audit reads the same single derivation the deploy renders from."""
    from libs.probe_specs import render_probe_spec_text

    specs: dict[str, str] = {}
    for line in render_probe_spec_text().splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        fields = [field.strip() for field in line.split("|")]
        specs[fields[0]] = fields[7] if len(fields) > 7 else ""
    return specs


def _worker_keys() -> tuple[
    set[tuple[str, str]], set[tuple[str, str]], dict[tuple[str, str], str]
]:
    config = tomllib.loads(WRANGLER.read_text(encoding="utf-8"))
    vars_config = config.get("vars", {})
    targets = json.loads(vars_config.get("WATCHDOG_TARGETS_JSON", "[]"))
    heartbeats = json.loads(vars_config.get("WATCHDOG_HEARTBEATS_JSON", "[]"))
    target_keys = {(target["environment"], target["name"]) for target in targets}
    heartbeat_keys = {
        (heartbeat["environment"], heartbeat["name"]) for heartbeat in heartbeats
    }
    identities = {
        (item["environment"], item["name"]): str(item.get("service_id", ""))
        for item in targets + heartbeats
    }
    return target_keys, heartbeat_keys, identities


def _github_signal_names() -> set[str]:
    module = _load_module("out_of_band_watchdog", OUT_OF_BAND)
    http_targets = module.parse_http_targets("")
    ssh_targets = module.parse_ssh_targets("")
    names = {target.name for target in http_targets}
    names.update(target.name for target in ssh_targets)
    names.add("cloudflare-worker-status")
    return names


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    raise SystemExit(main())
