#!/usr/bin/env python3
"""Audit watchdog signal ownership against live code-owned configs."""

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
COMPOSE = ROOT / "platform/12.alerting/compose.yaml"
WRANGLER = ROOT / "cloudflare/infra-watchdog/wrangler.toml"
OUT_OF_BAND = ROOT / "tools/out_of_band_watchdog.py"

VALID_OWNERS = {"internal", "cloudflare", "github", "excluded"}


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
    return yaml.safe_load(INVENTORY.read_text(encoding="utf-8"))


def _compose_probe_specs() -> dict[str, str]:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    raw_specs = compose["services"]["infra-probe-runner"]["environment"][
        "INFRA_PROBE_SPECS"
    ]
    specs: dict[str, str] = {}
    for line in raw_specs.splitlines():
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
    names.add("infra2-dokploy-route-canary")
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
