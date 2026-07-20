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

import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.vault_self_refresh_audit import (  # noqa: E402
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


def main() -> int:
    env = os.environ.get("VAULT_SELF_REFRESH_AUDIT_ENV", "production")
    report = run(env)
    print(write_report(report))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
