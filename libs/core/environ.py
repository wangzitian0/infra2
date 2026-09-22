"""Infra2 Core Environment & Configuration SSOT."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from libs.core.constants import PRODUCTION, STAGING


@dataclass(frozen=True)
class DeploymentEnvironment:
    """Strong-typed operational deployment environment entity (C-03)."""

    name: str = STAGING
    env_suffix: str = "-staging"
    internal_domain: str = "zitian.party"
    raw_env: Mapping[str, str | None] | None = None

    @property
    def is_production(self) -> bool:
        return self.name == PRODUCTION

    @property
    def is_staging(self) -> bool:
        return self.name == STAGING

    def get(self, key: str, default: str | None = None) -> str | None:
        if self.raw_env is not None and key in self.raw_env:
            val = self.raw_env[key]
            return val if val is not None else default
        return default

    def __getitem__(self, key: str) -> str | None:
        return self.get(key)


def get_environment() -> DeploymentEnvironment:
    """C-03: Retrieve current DeploymentEnvironment as a typed domain object."""
    from libs.common import get_env

    raw = get_env()
    env_name = raw.get("ENV") or STAGING
    env_suffix = raw.get("ENV_SUFFIX")
    if env_suffix is None:
        env_suffix = "" if env_name == PRODUCTION else f"-{env_name}"
    internal_domain = raw.get("INTERNAL_DOMAIN") or "zitian.party"
    return DeploymentEnvironment(
        name=env_name,
        env_suffix=env_suffix,
        internal_domain=internal_domain,
        raw_env=raw,
    )


def with_env_suffix(
    name: str, env: DeploymentEnvironment | Mapping[str, str | None]
) -> str:
    """C-04: Append environment suffix to service/container name."""
    if isinstance(env, DeploymentEnvironment):
        suffix = env.env_suffix
        return f"{name}{suffix}" if suffix else name

    from libs.common import with_env_suffix as _legacy_with_env_suffix

    return _legacy_with_env_suffix(name, env)


__all__ = [
    "DeploymentEnvironment",
    "get_environment",
    "with_env_suffix",
]
