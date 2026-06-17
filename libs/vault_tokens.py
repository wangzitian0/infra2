"""Vault per-service naming helpers (shared by the AppRole setup path).

The legacy static-token *ledger* (accessor bookkeeping, periodic-token output
masking) was retired once every service moved to AppRole auth — see
docs/ssot/bootstrap.iac_runner.md §6.4. Only the project/service/policy naming
helpers remain, reused by `bootstrap/05.vault/tasks.py::setup_approle`.
"""

from __future__ import annotations

from dataclasses import dataclass

from libs.common import normalize_env_name


@dataclass(frozen=True)
class VaultTokenTarget:
    project: str
    service: str
    service_dir: str
    project_dir: str
    dokploy_project: str


def normalize_selector(value: str | None, *, label: str) -> str | None:
    """Normalize an optional project/service selector."""
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} must not be empty")
    if "-" in normalized or "/" in normalized:
        raise ValueError(f"{label} must not include '-' or '/'")
    return normalized


def policy_name(project: str, env: str, service: str) -> str:
    """Return the per-environment Vault policy name for a service's AppRole."""
    env_name = normalize_env_name(env)
    return f"{project}-{env_name}-{service}"
