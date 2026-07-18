#!/usr/bin/env python3
"""Bespoke-app compose_id drift detector (#524).

``libs/deploy_env_config.py`` hardcodes each bespoke app's Dokploy ``compose_id`` as a
literal string, read once from the live API and never re-verified. If the underlying
Dokploy compose is ever deleted and recreated (e.g. someone recreates the "app" compose
in the Dokploy UI), it gets a fresh internal ``composeId`` and the literal here goes
stale. ``libs/deploy/promote.py``'s first Dokploy call already fails loud (a 404) when
that happens — but only AT deploy time, with no proactive signal beforehand.

This is that proactive signal: for every ``libs.deploy_env_config.bespoke_app_compose_
targets()`` entry, re-resolve the compose by (project, env, name) via ``DokployClient.
find_compose_by_name`` — the same call ``libs/vault_self_refresh_audit.py`` and
``libs/deploy/preview.py`` already use — and assert the live ``composeId`` still matches
the hardcoded literal.

READ-ONLY (Dokploy GETs only; no deploy, no writes). Recovery path when this fails: see
the module docstring in ``libs/deploy_env_config.py``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.deploy_env_config import (  # noqa: E402
    ComposeTarget,
    bespoke_app_compose_targets,
)


@dataclass
class Row:
    target: ComposeTarget
    verdict: str  # "ok" | "DRIFT" | "missing" | "error"
    live_compose_id: str | None = None
    note: str = ""


def check_target(client, target: ComposeTarget) -> Row:
    """Re-resolve `target` live and compare against its hardcoded compose_id."""
    try:
        compose = client.find_compose_by_name(
            target.compose_name, target.project_name, env_name=target.dokploy_env_name
        )
    except Exception as exc:  # noqa: BLE001 — a lookup failure is not proof of drift
        return Row(target, "error", note=f"live lookup failed: {exc}")
    if compose is None:
        return Row(
            target,
            "missing",
            note=(
                f"no compose named {target.compose_name!r} found in Dokploy project "
                f"{target.project_name!r} env {target.dokploy_env_name!r} "
                f"(hardcoded compose_id={target.compose_id!r})"
            ),
        )
    live_id = compose.get("composeId")
    if live_id != target.compose_id:
        return Row(
            target,
            "DRIFT",
            live_compose_id=live_id,
            note=(
                f"libs/deploy_env_config.py hardcodes compose_id={target.compose_id!r} "
                f"for {target.service} env={target.env!r}, but the live Dokploy compose "
                f"{target.project_name}/{target.dokploy_env_name}/{target.compose_name} "
                f"now has composeId={live_id!r}. Update the hardcoded literal to "
                f"{live_id!r}."
            ),
        )
    return Row(target, "ok", live_compose_id=live_id)


def scan(client) -> list[Row]:
    return [check_target(client, target) for target in bespoke_app_compose_targets()]


def format_report(rows: list[Row]) -> str:
    drift = [r for r in rows if r.verdict == "DRIFT"]
    missing = [r for r in rows if r.verdict == "missing"]
    errors = [r for r in rows if r.verdict == "error"]
    lines = [
        f"📋 [Infra2] bespoke-app compose_id drift · {len(rows)} target(s) checked "
        f"· DRIFT {len(drift)} · missing {len(missing)} · error {len(errors)}",
    ]
    for r in drift:
        lines.append(f"🔴 DRIFT {r.target.service} ({r.target.env}): {r.note}")
    for r in missing:
        lines.append(f"🔴 MISSING {r.target.service} ({r.target.env}): {r.note}")
    for r in errors:
        lines.append(f"⚠️ ERROR {r.target.service} ({r.target.env}): {r.note}")
    if not drift and not missing and not errors:
        lines.append("✅ every hardcoded compose_id matches its live Dokploy compose.")
    return "\n".join(lines)


def blockers(rows: list[Row]) -> list[Row]:
    """Rows that fail the check (drift, a compose that vanished, or an unresolved lookup)."""
    return [r for r in rows if r.verdict != "ok"]


def main() -> int:
    from libs.dokploy import get_dokploy

    client = get_dokploy()
    rows = scan(client)
    report = format_report(rows)
    print(report)
    return 1 if blockers(rows) else 0


if __name__ == "__main__":
    sys.exit(main())
