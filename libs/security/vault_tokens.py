"""Vault per-service naming helpers (shared by the AppRole setup path).

Canonical implementation lives in ``infra2_sdk.secrets`` (#1270).
Includes a stdlib fallback for bare-interpreter CI jobs (``ops-checks.yml``).
"""

from __future__ import annotations

from dataclasses import dataclass

from common.environ import normalize_env_name

try:
    from infra2_sdk.secrets import (
        VaultTokenTarget,
        normalize_selector,
        policy_name,
    )
except ImportError:  # pragma: no cover - bare interpreter without infra2-sdk installed

    @dataclass(frozen=True)
    class VaultTokenTarget:  # type: ignore[no-redef]
        project: str
        service: str
        service_dir: str
        project_dir: str
        dokploy_project: str

    def normalize_selector(value: str | None, *, label: str) -> str | None:  # type: ignore[misc]
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{label} must not be empty")
        if "-" in normalized or "/" in normalized:
            raise ValueError(f"{label} must not include '-' or '/'")
        return normalized

    def policy_name(project: str, env: str, service: str) -> str:  # type: ignore[misc]
        env_name = normalize_env_name(env)
        return f"{project}-{env_name}-{service}"


__all__ = [
    "VaultTokenTarget",
    "normalize_selector",
    "policy_name",
]
