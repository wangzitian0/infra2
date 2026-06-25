"""Bind the watchdog-signals inventory's exclusion discipline to the service registry.

A staging PUBLIC-ROUTE signal (env-suffixed host, e.g. signoz-staging.zitian.party) for a
prod_only service points at a host that never exists — it must be a documented exclusion
(`primary_owner: excluded`), not an active signal. This locks the staging.signoz.public-route
exclusion whose own `revalidation_condition` warns "Do not re-enable — signoz is global-by-
design", so the registry's prod_only flag, not human vigilance, keeps it excluded.

Scope note (why this is narrow, learned by inspection): INTERNAL probes are NOT covered — they
target the UNSUFFIXED prod host (`platform-signoz`, no ${ENV_SUFFIX}, per #430), so a staging
probe-runner hits the real prod instance and is legitimately active. Only url-bearing public
routes carry the env-suffixed host that can vanish for a prod_only service.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from libs import service_registry

ROOT = Path(__file__).resolve().parents[2]
SIGNALS = ROOT / "docs/ssot/watchdog-signals.yaml"


def test_staging_public_route_signals_for_prod_only_services_are_excluded() -> None:
    signals = yaml.safe_load(SIGNALS.read_text(encoding="utf-8"))["signals"]
    assert signals, "no signals parsed from watchdog-signals.yaml (parser drift?)"

    problems: list[str] = []
    checked = 0
    for sig in signals:
        if "url" not in sig:  # only public routes carry an env-suffixed host
            continue
        if sig.get("environment") == "production":
            continue
        meta = service_registry.resolve_container_host(f"platform-{sig.get('component')}")
        if meta is None or not meta.prod_only:
            continue  # bootstrap / per-env service -> a staging host legitimately exists
        checked += 1
        if sig.get("primary_owner") != "excluded":
            problems.append(
                f"{sig.get('signal_id')}: staging public route for prod_only "
                f"'{meta.service_id}' targets a host that never exists — must be "
                f"primary_owner: excluded with a reason, not an active signal"
            )

    assert checked, "no staging prod_only public-route signals resolved (mapping drift?)"
    assert not problems, "\n".join(problems)
