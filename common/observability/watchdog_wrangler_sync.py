"""Derive Cloudflare Watchdog Worker target/heartbeat JSON from watchdog-signals.yaml."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import yaml

from common.core.registry import service_id_for_component

ROOT = Path(__file__).resolve().parents[2]
SIGNALS_PATH = ROOT / "docs/ssot/watchdog-signals.yaml"
REPOS_PATH = ROOT / "harness/repos.yaml"
WRANGLER_PATH = ROOT / "cloudflare/infra-watchdog/wrangler.toml"

_DURATION_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_SERVICE_TO_HARNESS_ID = {
    "finance_report/app": "finance-report",
    "truealpha/app": "truealpha",
}


def _parse_duration_seconds(raw: object, *, signal_id: str) -> int:
    text = str(raw or "").strip()
    match = re.fullmatch(r"(\d+)([smhd])", text)
    if not match:
        raise ValueError(f"{signal_id}: invalid stale_threshold {raw!r}")
    return int(match.group(1)) * _DURATION_MULTIPLIERS[match.group(2)]


def _parse_statuses(raw: object, *, signal_id: str) -> list[int]:
    parts = [part.strip() for part in str(raw or "").split(",") if part.strip()]
    if not parts or not all(part.isdigit() for part in parts):
        raise ValueError(f"{signal_id}: invalid expected statuses {raw!r}")
    return [int(part) for part in parts]


def _harness_production_health(repos_path: Path) -> dict[str, str]:
    if not repos_path.is_file():
        return {}
    doc = yaml.safe_load(repos_path.read_text(encoding="utf-8")) or {}
    out: dict[str, str] = {}
    for entry in doc.get("repositories") or ():
        repo_id = str(entry.get("id") or "")
        health = entry.get("health") or {}
        if repo_id and isinstance(health, dict) and health.get("production"):
            out[repo_id] = str(health["production"])
    return out


def build_worker_vars(
    *,
    signals_path: Path = SIGNALS_PATH,
    repos_path: Path = REPOS_PATH,
) -> tuple[list[dict], list[dict]]:
    """Return ``(targets, heartbeats)`` derived from SSOT signals and harness repos."""
    inventory = yaml.safe_load(signals_path.read_text(encoding="utf-8")) or {}
    signals = inventory.get("signals") or []
    harness_prod = _harness_production_health(repos_path)

    targets: list[dict] = []
    heartbeats: list[dict] = []

    for signal in signals:
        if signal.get("primary_owner") != "cloudflare":
            continue
        signal_id = str(signal.get("signal_id") or "")
        env = str(signal.get("environment") or "")
        name = str(signal.get("signal") or "")
        component = str(signal.get("component") or "")
        severity = str(signal.get("severity") or "critical")
        service_id = service_id_for_component(component, signal=name)

        if name.endswith("public-route"):
            url = str(signal.get("url") or "")
            if not url:
                raise ValueError(f"{signal_id}: missing url")
            harness_id = _SERVICE_TO_HARNESS_ID.get(service_id)
            if harness_id and harness_id in harness_prod:
                expected_url = harness_prod[harness_id]
                if urlparse(url).netloc != urlparse(expected_url).netloc:
                    raise ValueError(
                        f"{signal_id}: host {url!r} disagrees with harness/repos.yaml {expected_url!r}"
                    )
            targets.append(
                {
                    "environment": env,
                    "name": name,
                    "service_id": service_id,
                    "url": url,
                    "statuses": _parse_statuses(
                        signal.get("expected"), signal_id=signal_id
                    ),
                    "severity": severity,
                }
            )
            continue

        hb: dict = {
            "environment": env,
            "name": name,
            "service_id": service_id,
            "maxAgeSeconds": _parse_duration_seconds(
                signal.get("stale_threshold"), signal_id=signal_id
            ),
        }
        if signal.get("min_write_interval_seconds") is not None:
            hb["minWriteIntervalSeconds"] = int(signal["min_write_interval_seconds"])
        if signal.get("status_change_writes_per_day") is not None:
            hb["statusChangeWritesPerDay"] = int(signal["status_change_writes_per_day"])
        if signal.get("failure_class") == "ops-scheduler":
            hb["failureClass"] = "ops-scheduler"
        hb["severity"] = severity
        heartbeats.append(hb)

    if not targets:
        raise ValueError("derived WATCHDOG_TARGETS_JSON is empty")
    if not heartbeats:
        raise ValueError("derived WATCHDOG_HEARTBEATS_JSON is empty")
    return targets, heartbeats


def render_json_array(items: list[dict]) -> str:
    """Format a compact one-object-per-line JSON array matching wrangler.toml."""
    lines = [json.dumps(item, separators=(",", ":")) for item in items]
    return "[\n" + ",\n".join(f"  {line}" for line in lines) + "\n]"


def render_wrangler_text(
    wrangler_text: str,
    *,
    signals_path: Path = SIGNALS_PATH,
    repos_path: Path = REPOS_PATH,
) -> str:
    """Replace ``WATCHDOG_TARGETS_JSON`` and ``WATCHDOG_HEARTBEATS_JSON`` blocks."""
    targets, heartbeats = build_worker_vars(
        signals_path=signals_path, repos_path=repos_path
    )
    replacements = {
        "WATCHDOG_TARGETS_JSON": render_json_array(targets),
        "WATCHDOG_HEARTBEATS_JSON": render_json_array(heartbeats),
    }
    updated = wrangler_text
    for key, body in replacements.items():
        pattern = re.compile(
            rf'({re.escape(key)}\s*=\s*""")\n.*?\n(""")',
            re.DOTALL,
        )
        if not pattern.search(updated):
            raise ValueError(f"wrangler.toml missing triple-quoted {key} block")
        updated = pattern.sub(rf"\1\n{body}\n\2", updated, count=1)
    return updated


def check_or_write_wrangler(
    *,
    write: bool = False,
    wrangler_path: Path = WRANGLER_PATH,
    signals_path: Path = SIGNALS_PATH,
    repos_path: Path = REPOS_PATH,
) -> bool:
    """Return True when ``wrangler.toml`` matches SSOT (after writing if ``write=True``)."""
    current = wrangler_path.read_text(encoding="utf-8")
    rendered = render_wrangler_text(
        current, signals_path=signals_path, repos_path=repos_path
    )
    if current == rendered:
        return True
    if write:
        wrangler_path.write_text(rendered, encoding="utf-8")
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Exit 0 iff synced")
    mode.add_argument(
        "--write", action="store_true", help="Sync wrangler.toml in place"
    )
    args = parser.parse_args(argv)
    if args.write:
        check_or_write_wrangler(write=True)
        print("wrangler.toml synced with watchdog-signals.yaml")
        return 0
    if check_or_write_wrangler(write=False):
        print("wrangler.toml matches watchdog-signals.yaml")
        return 0
    print(
        "ERROR: cloudflare/infra-watchdog/wrangler.toml drifted from watchdog-signals.yaml; "
        "run python -m tools.watchdog_wrangler_sync --write"
    )
    return 1
