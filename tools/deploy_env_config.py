#!/usr/bin/env python3
"""Per-environment deploy config — the ``env`` axis of deploy(env, code, data).

The deploy primitive (finance_report#883) is parameterized by three axes:

    code  -> resolve_deploy_ref.py        (a surface input -> a commit sha)
    env   -> THIS module                  (which compose, URL, suffix, data default)
    data  -> (P3/#893)                    (empty / staging / anonymized prod snapshot)

This module owns the ``env`` axis: each deploy environment maps to the Dokploy compose
it targets, its public URL pattern, the container/domain suffix, the default data
source, and prod gating. Single source so the deploy primitive, docs, and the contract
test all derive from here instead of re-stating per-env values across workflows.

The compose ids are mirrored from the App-repo workflows (staging-deploy.yml /
production-release.yml) and become the sole copy once P2 step 5 removes them there.
No deploy is performed here — like the resolver, this is pure, importable config.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

# data default per env. Non-prod defaults to `staging` data (operator choice); prod is
# always real prod data. A PR sha never runs on prod data and prod data never leaves
# prod un-anonymized (the G2 / RL-DATA red lines, finance_report#877) — those are
# enforced at the data axis (P3), not here.
_DATA_STAGING = "staging"
_DATA_PROD = "prod"


@dataclass(frozen=True)
class EnvConfig:
    name: str
    compose_id: str | None  # None = dynamic per-PR (preview, looked up by lifecycle)
    env_suffix: str
    app_url_pattern: str  # contains {domain}, and {number} for the dynamic preview env
    data_default: str
    gates_prod: bool = False  # prod must deploy here first (staging)
    requires_staging_first: bool = False  # this env (prod) requires a staging deploy
    dynamic: bool = False  # per-PR; compose_id/suffix are resolved at deploy time

    def app_url(self, *, domain: str, number: int | str | None = None) -> str:
        """Concrete public URL: fill {domain} (always) and {number} (preview only)."""
        return self.app_url_pattern.format(
            domain=domain, number="" if number is None else number
        )


_ENVIRONMENTS: dict[str, EnvConfig] = {
    "staging": EnvConfig(
        name="staging",
        compose_id="A6V-hbJlgHMwgPDoTDnhH",
        env_suffix="-staging",
        app_url_pattern="https://report-staging.{domain}",
        data_default=_DATA_STAGING,
        gates_prod=True,
    ),
    "prod": EnvConfig(
        name="prod",
        compose_id="lNn9gVS1Zyw79Jzw5dlbu",
        env_suffix="",
        app_url_pattern="https://report.{domain}",
        data_default=_DATA_PROD,
        requires_staging_first=True,
    ),
    "preview": EnvConfig(
        name="preview",
        compose_id=None,  # per-PR compose, created/looked up by the preview lifecycle
        env_suffix="",  # actually -pr-<number>, set per-PR at deploy time
        app_url_pattern="https://report-pr-{number}.{domain}",
        data_default=_DATA_STAGING,
        dynamic=True,
    ),
}

ENVIRONMENTS = tuple(_ENVIRONMENTS)


def env_config(env: str) -> EnvConfig:
    """Return the EnvConfig for a deploy env. Raises ValueError for an unknown env."""
    try:
        return _ENVIRONMENTS[env]
    except KeyError:
        raise ValueError(
            f"unknown deploy env {env!r}: expected one of {sorted(_ENVIRONMENTS)}"
        ) from None


def for_env_suffix(env: str, *, number: int | str | None = None) -> str:
    """The container/domain ENV_SUFFIX for a deploy env (preview is -pr-<number>)."""
    cfg = env_config(env)
    if cfg.dynamic and number is not None:
        return f"-pr-{number}"
    return cfg.env_suffix


def with_compose_id(env: str, compose_id: str) -> EnvConfig:
    """Bind a runtime compose_id (used for the dynamic preview env)."""
    return replace(env_config(env), compose_id=compose_id)
