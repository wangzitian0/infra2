#!/usr/bin/env python3
"""Fail-closed audit for the deploy/runtime/telemetry/alert identity contract."""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.service_identity import ServiceIdentity  # noqa: E402
from libs.service_registry import all_services, service_attrs  # noqa: E402
from tools.watchdog_consistency_audit import audit as audit_watchdog  # noqa: E402

DEPLOY_BOUNDARIES = (
    ROOT / "libs/deploy/deployer.py",
    ROOT / "libs/deploy/promote.py",
    ROOT / "libs/deploy/preview.py",
)


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

    for path in ROOT.glob("**/observability/alert_rules.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        service_id = str(data.get("service_id", ""))
        environment = str(data.get("environment", ""))
        if service_id not in registered:
            errors.append(
                f"alert catalog has unknown service_id {service_id!r}: {path.relative_to(ROOT)}"
            )
            continue
        try:
            ServiceIdentity.build(service_id, environment)
        except ValueError as exc:
            errors.append(
                f"invalid alert catalog identity {path.relative_to(ROOT)}: {exc}"
            )

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
