#!/usr/bin/env python3
"""Scheduled CI wrapper for the Vault self-refresh audit (#531).

``libs/vault_self_refresh_audit.py`` was a manual-only operator tool (``invoke
vault-audit.self-refresh``) since it was built in #166/#526 -- nothing ever forced it
to run, which is exactly how both of #531's structural bugs (``classify_token``
hardcoding the legacy ``vault_token_env_key`` a month after the fleet finished
migrating to AppRole, and ``classify_rendered_env``'s mtime-staleness check firing on
every healthy, low-secret-churn service) sat undetected for a month+.

This wrapper gives the audit a scheduled forcing function, mirroring
``tools/app_compose_id_drift.py``'s (#524) shape: call the library functions directly
(no ``invoke`` wrapper -- this runs on a GitHub Actions runner, not inside the VPS
where ``invoke vault-audit.self-refresh`` normally runs, so ``tools/vault_audit.py``'s
task decorator isn't the right surface here). ``collect_live_observations`` needs SSH
access to the VPS to inspect vault-agent/app containers; the GitHub Actions job
provisions this via the same ``INFRA2_WATCHDOG_SSH_*`` secrets the
watchdog jobs already use (see ``.github/workflows/ops-checks.yml`` and
``libs/vault_self_refresh_audit.py::_ssh``'s CI-override env vars) -- no new secret.

READ-ONLY: ``collect_live_observations`` only GETs Dokploy compose env and SSHes into
the VPS to run ``docker inspect``/``docker exec cat``/``docker logs`` -- it never
mutates, restarts, or rotates anything.

Exit code mirrors ``report["status"]``: the step fails (exit 1) on ANY non-pass result
-- including a transient SSH/Dokploy lookup hiccup -- so it's visible in the run log
and retried on tomorrow's schedule. But ``audit_from_observations``'s overall status
already treats "info" results (e.g. #526's optional-field-inertness note, or #531's
demoted rendered-env staleness note) as non-gating, so a non-pass status here already
means at least one real ``fail`` result exists -- never a bare info note. The caller
(the CI alert step) additionally filters to ``status == "fail"`` results before paging
Feishu, so alerting is never triggered by an "info" result (see #524/#425/#475 for why
alerting only on a confirmed signal, not a transient blip, matters).
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.security.vault_self_refresh_audit import (  # noqa: E402
    audit_from_observations,
    collect_live_observations,
    inventory_ids_not_in_production,
    load_inventory,
    write_report,
)


def run(env: str = "production") -> dict[str, Any]:
    """Collect live observations and classify them. READ-ONLY.

    A production run excludes inventory entries with no production deployment
    yet — otherwise the daily audit pages Feishu forever on the same
    non-actionable "missing from production" finding (verified live: #500
    scoped truealpha's rollout to staging only; the truealpha Dokploy project's
    `production` environment has zero composes). The exclusion set is DERIVED
    from deploy-side facts (#542) — the Deployer `not_yet_in_production` attr
    and `_APP_COMPOSE_OVERRIDES`' prod compose_id=None — never a hand-kept
    list, so promoting a service to production updates the audit automatically.
    """
    services = load_inventory()
    if env == "production":
        excluded = inventory_ids_not_in_production()
        services = [s for s in services if s.id not in excluded]
    observations = collect_live_observations(services, env=env)
    return audit_from_observations(services, observations, env=env)


def confirmed_finding_pairs(report: dict[str, Any]) -> list[tuple[str, str]]:
    """``(identity key, display line)`` per confirmed ``fail`` result (#962).

    The key is ``service_id::check_id`` -- no summary, which carries ages and
    counts -- so the same failing check is the same finding every day. ``info``
    results never page (#531).
    """
    return [
        (
            f"{r['service_id']}::{r['check_id']}",
            f"- {r['severity']} {r['service_id']}::{r['check_id']} - {r['summary']}",
        )
        for r in report["results"]
        if r["status"] == "fail"
    ]


#: Where `main()` leaves the report, for the step that resolves a paged failure after
#: a green run (#962). Job-level env; absent = not recorded.
REPORT_ENV = "VAULT_SELF_REFRESH_AUDIT_REPORT"
#: Checks that answer `info` ("could not compare") instead of a verdict when they
#: could not look, and `fail` when they could: an `info` from one of these is not a
#: pass. `libs/tests/test_page_dedup.py` ties this to `classify_deployed_template`.
CAN_FAIL_BUT_REPORT_INFO = ("deployed-template",)


def unevaluated_keys(report: dict[str, Any]) -> list[str]:
    """Key prefixes of checks that could not tell (#962): `info` from a check that
    can also `fail`. The audit passes with them, but a finding paged for that check
    has not recovered, it is unknown."""
    return [
        f"{r['service_id']}::{r['check_id']}"
        for r in report["results"]
        if r["status"] == "info" and r["check_id"] in CAN_FAIL_BUT_REPORT_INFO
    ]


def read_unevaluated(env: Mapping[str, str]) -> list[str]:
    """The prefixes the last `main()` run could not evaluate. No readable passing
    report is everything (``[""]``): without proof of a check, nothing resolves."""
    path = (env.get(REPORT_ENV) or "").strip()
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        if report["status"] == "pass":
            return unevaluated_keys(report)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return [""]


def main() -> int:
    env = os.environ.get("VAULT_SELF_REFRESH_AUDIT_ENV", "production")
    report = run(env)
    print(write_report(report))
    if path := os.environ.get(REPORT_ENV, "").strip():
        try:
            Path(path).write_text(json.dumps(report), encoding="utf-8")
        except OSError as exc:
            print(f"could not record the report: {exc}", file=sys.stderr)
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
