#!/usr/bin/env python3
"""The commit-addressed deploy primitive: deploy(env, code, data).

Assembles the two pure axes — resolve_deploy_ref (code -> sha) and deploy_env_config
(env -> compose/url/suffix/data-default) — and drives the existing Dokploy call layer
(update the compose env to the digest, then deploy). One primitive, three configs
(preview/staging/prod); finance_report#883 P2 step 3.

This is the first piece with side effects: it mutates a Dokploy compose's env and
triggers a deploy. The Dokploy client is injected so it is unit-testable without a live
control plane. Rollout/readiness polling and the data-axis side effects (P3/#893) are
layered on by the callers (step 4); this owns assembly + trigger + the gates.
"""

from __future__ import annotations

from dataclasses import dataclass

from tools.deploy_env_config import env_config
from tools.resolve_deploy_ref import resolve_to_sha


@dataclass(frozen=True)
class DeployPlan:
    env: str
    sha: str
    compose_id: str
    data: str
    env_vars: dict[str, str]


def deploy(
    env: str,
    code: str,
    *,
    domain: str,
    client,
    data: str | None = None,
    staging_validated: bool = False,
    break_glass: bool = False,
    repo: str | None = None,
) -> DeployPlan:
    """Deploy a commit to an environment.

    code  -> resolve_deploy_ref (main / release/x.y / vX.Y.Z / <sha>) -> a commit sha.
    env   -> deploy_env_config (which compose, URL, suffix, default data).
    data  -> defaults to the env's data_default (P3 owns the data side effects).

    Gating:
    - The dynamic preview env has no fixed compose; bind one via the preview lifecycle.
    - An env that requires-staging-first (prod) refuses to deploy a digest that has not
      been validated on staging (promote-not-rebuild), unless break_glass=True — an
      audited override (H5). staging_validated is supplied by the caller that checked a
      staging deploy of this exact sha ran.
    """
    # Explicit None checks: an empty string is a caller error, not a silent fallback.
    if not domain or any(c.isspace() for c in domain):
        raise ValueError(
            f"invalid domain {domain!r}: must be non-empty with no whitespace "
            "(it is interpolated into a line-based compose env file)."
        )
    sha = resolve_to_sha(code, repo=repo) if repo is not None else resolve_to_sha(code)
    cfg = env_config(env)
    data = data if data is not None else cfg.data_default

    if cfg.dynamic:
        raise ValueError(
            f"{env!r} is a per-PR dynamic env with no fixed compose; bind a compose_id "
            "via the preview lifecycle instead of calling deploy() directly."
        )
    if cfg.requires_staging_first and not staging_validated and not break_glass:
        raise ValueError(
            f"{env!r} requires a staging deploy of digest {sha} first "
            "(promote-not-rebuild). Pass staging_validated=True once staging has run "
            "this exact digest, or break_glass=True as an audited override (H5)."
        )

    env_vars = {
        "IMAGE_TAG": sha,
        "GIT_COMMIT_SHA": sha,
        "NEXT_PUBLIC_APP_URL": cfg.app_url(domain=domain),
        "ENV_SUFFIX": cfg.env_suffix,
        "ENV_DOMAIN_SUFFIX": cfg.env_suffix,
    }
    # update_compose_env merges with the existing compose env (keeps runtime-injected
    # secrets/AppRole creds); we only override the digest + URL + suffix.
    client.update_compose_env(cfg.compose_id, env_vars=env_vars)
    client.deploy_compose(cfg.compose_id)

    return DeployPlan(
        env=env, sha=sha, compose_id=cfg.compose_id, data=data, env_vars=env_vars
    )
