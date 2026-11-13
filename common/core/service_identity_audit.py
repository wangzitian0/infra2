#!/usr/bin/env python3
"""Fail-closed audit for the deploy/runtime/telemetry/alert identity contract."""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.core.service_identity import ServiceIdentity  # noqa: E402
from common.alerting.standard_rules import ENVIRONMENTS, STANDARD_RULES  # noqa: E402
from common.core.registry import all_services, service_attrs  # noqa: E402
from common.observability.watchdog_audit import audit as audit_watchdog  # noqa: E402

DEPLOY_BOUNDARIES = (
    ROOT / "common/deploy/deployer.py",
    ROOT / "common/deploy/promote.py",
    ROOT / "common/deploy/preview.py",
)
NON_OWNED_TOP_LEVEL = {"repos", "oh-my-code-agent"}


def _owned_alert_catalogs() -> list[Path]:
    """Return root-owned catalogs, excluding nested checkouts and tool worktrees."""
    catalogs: list[Path] = []
    for path in ROOT.glob("**/observability/alert_rules.json"):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in NON_OWNED_TOP_LEVEL:
            continue
        if any(part.startswith(".") for part in relative.parts):
            continue
        catalogs.append(path)
    return sorted(catalogs)


def audit() -> list[str]:
    errors: list[str] = []
    registered = set(all_services())

    for service_id, meta in service_attrs().items():
        for environment in ("production", "staging"):
            try:
                ServiceIdentity.build(
                    service_id,
                    environment,
                    component=meta.telemetry_component or meta.service,
                    service_name=meta.telemetry_service_name or meta.service,
                )
            except ValueError as exc:
                errors.append(f"invalid registry identity {service_id}: {exc}")

    for path in DEPLOY_BOUNDARIES:
        source = path.read_text(encoding="utf-8")
        for marker in ("ServiceIdentity.build", ".deploy_env()"):
            if marker not in source:
                errors.append(
                    f"deployment boundary lacks {marker}: {path.relative_to(ROOT)}"
                )

    catalogs = [
        (str(path.relative_to(ROOT)), json.loads(path.read_text(encoding="utf-8")))
        for path in _owned_alert_catalogs()
    ]
    catalogs.append(("common/alerting/standard_rules.py", {"rules": STANDARD_RULES}))
    for label, data in catalogs:
        # Rules of the standard table carry their own service_id; a catalog has one.
        service_ids = {
            str(data.get("service_id") or rule.get("service_id", ""))
            for rule in data["rules"]
        }
        for service_id in sorted(service_ids):
            if service_id not in registered:
                errors.append(
                    f"alert catalog has unknown service_id {service_id!r}: {label}"
                )
                continue
            for environment in ENVIRONMENTS:
                try:
                    ServiceIdentity.build(service_id, environment)
                except ValueError as exc:
                    errors.append(f"invalid alert catalog identity {label}: {exc}")

    errors.extend(f"watchdog: {error}" for error in audit_watchdog())
    return errors


def main() -> int:
    errors = audit()
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("service identity audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
