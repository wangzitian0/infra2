"""Contract (#163): every platform service with a container_name has a healthcheck.

A service without a healthcheck reports "running" while its process is hung. Docker,
Dokploy, and the watchdogs then see a healthy container and nothing restarts it or
raises an alert. This guard fails when a `platform/*/compose.yaml` service that has a
`container_name` declares no usable healthcheck.

Exemptions are explicit and shrink-only. Each exemption carries a reason. The guard
fails on a stale exemption (service removed, renamed, or now healthchecked), so the
list can only get shorter. Do not add a service to the list to silence the guard:
add a healthcheck instead.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]

# A one-shot job runs to completion and exits (an init or a migrator). A healthcheck
# does not apply to it. It is derived, not listed: its restart policy is "no", or
# another service waits for it with service_completed_successfully. "on-failure" is
# not enough: platform/23.prefect's long-running worker uses it.
_ONE_SHOT_RESTART = frozenset({"no"})
# The one-shot set as of #163. A change to it is a reviewed change to this test: a
# long-running service that sets restart: "no" must not slip past the guard.
EXPECTED_ONE_SHOTS = frozenset(
    {
        ("platform/03.clickhouse/compose.yaml", "init-clickhouse"),
        ("platform/10.authentik/compose.yaml", "token-init"),
        ("platform/11.signoz/compose.yaml", "schema-migrator"),
    }
)

# (compose path relative to ROOT, service name) -> reason the service has no healthcheck.
# Shrink-only: remove an entry in the same change that adds the healthcheck.
HEALTHCHECK_EXEMPTIONS: dict[tuple[str, str], str] = {}


def _declares_healthcheck(service: dict) -> bool:
    """True when the service sets a healthcheck that Docker actually runs."""
    healthcheck = service.get("healthcheck")
    if not isinstance(healthcheck, dict):
        return False
    if healthcheck.get("disable") is True:
        return False
    test = healthcheck.get("test")
    if not test:
        return False
    # `test: ["NONE"]` (or the string "NONE") turns the healthcheck off.
    first = test[0] if isinstance(test, list) else test
    return str(first).strip().upper() != "NONE"


def _named_services(compose: dict) -> dict[str, dict]:
    return {
        name: service
        for name, service in (compose.get("services") or {}).items()
        if isinstance(service, dict) and service.get("container_name")
    }


def _one_shot_jobs(compose: dict) -> set[str]:
    services = compose.get("services") or {}
    awaited = {
        dependency
        for service in services.values()
        if isinstance(service, dict) and isinstance(service.get("depends_on"), dict)
        for dependency, condition in service["depends_on"].items()
        if isinstance(condition, dict)
        and condition.get("condition") == "service_completed_successfully"
    }
    by_restart = {
        name
        for name, service in services.items()
        if isinstance(service, dict)
        and str(service.get("restart", "")).strip('"') in _ONE_SHOT_RESTART
    }
    return awaited | by_restart


def _platform_compose_files() -> list[Path]:
    return sorted((ROOT / "platform").glob("*/compose.yaml"))


def _load_named_services() -> dict[tuple[str, str], dict]:
    """Every long-running named platform service (one-shot jobs excluded)."""
    found: dict[tuple[str, str], dict] = {}
    for path in _platform_compose_files():
        compose = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        rel = path.relative_to(ROOT).as_posix()
        one_shots = _one_shot_jobs(compose)
        for name, service in _named_services(compose).items():
            if name not in one_shots:
                found[(rel, name)] = service
    return found


def _load_one_shots() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in _platform_compose_files():
        compose = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        rel = path.relative_to(ROOT).as_posix()
        found |= {(rel, name) for name in _one_shot_jobs(compose)}
    return found


def test_the_one_shot_jobs_are_the_reviewed_set() -> None:
    """A long-running service that gains restart: "no" would leave the guard's scope;
    this pins the derived set so that change has to be made here, on purpose."""
    assert _load_one_shots() == EXPECTED_ONE_SHOTS


def test_discovery_finds_platform_services() -> None:
    """Guard against GREEN-WHILE-EMPTY: a broken glob must not pass the guard."""
    found = _load_named_services()
    compose_files = {rel for rel, _ in found}
    assert "platform/30.todo/compose.yaml" in compose_files
    assert "platform/21.portal/compose.yaml" in compose_files
    assert len(found) >= 20, f"discovery found only {len(found)} named services"


def test_every_platform_service_declares_a_healthcheck_or_is_exempt() -> None:
    missing = sorted(
        key
        for key, service in _load_named_services().items()
        if not _declares_healthcheck(service) and key not in HEALTHCHECK_EXEMPTIONS
    )
    assert not missing, (
        "These platform services have a container_name but no healthcheck. Add a "
        "`healthcheck:` that uses a binary present in the image (#163):\n"
        + "\n".join(f"  {rel}: service {name!r}" for rel, name in missing)
    )


def test_healthcheck_exemptions_are_current_and_justified() -> None:
    found = _load_named_services()
    stale = sorted(
        key
        for key in HEALTHCHECK_EXEMPTIONS
        if key not in found or _declares_healthcheck(found[key])
    )
    assert not stale, (
        "These exemptions are stale (service removed or now has a healthcheck). "
        "Delete them from HEALTHCHECK_EXEMPTIONS so the list stays shrink-only:\n"
        + "\n".join(f"  {rel}: service {name!r}" for rel, name in stale)
    )
    unjustified = sorted(
        key for key, reason in HEALTHCHECK_EXEMPTIONS.items() if not reason.strip()
    )
    assert not unjustified, f"Exemptions without a reason: {unjustified}"


def test_declares_healthcheck_rejects_missing_and_disabled_forms() -> None:
    """Self-check: the detector must fail for every form that runs no check."""
    ok = {"healthcheck": {"test": ["CMD", "true"]}}
    assert _declares_healthcheck(ok)
    assert _declares_healthcheck({"healthcheck": {"test": "true"}})
    assert not _declares_healthcheck({})
    assert not _declares_healthcheck({"healthcheck": None})
    assert not _declares_healthcheck({"healthcheck": {}})
    assert not _declares_healthcheck({"healthcheck": {"interval": "5s"}})
    assert not _declares_healthcheck(
        {"healthcheck": {"disable": True, "test": ["CMD", "true"]}}
    )
    assert not _declares_healthcheck({"healthcheck": {"test": ["NONE"]}})
    assert not _declares_healthcheck({"healthcheck": {"test": "none"}})


def test_named_services_ignores_services_without_container_name() -> None:
    compose = {
        "services": {
            "named": {"container_name": "platform-x", "image": "x"},
            "anonymous": {"image": "y"},
        }
    }
    assert list(_named_services(compose)) == ["named"]
