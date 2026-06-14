#!/usr/bin/env python3
"""The commit-addressed deploy primitive: deploy(env, code, data).

Assembles the two pure axes — resolve_deploy_ref (code -> sha) and deploy_env_config
(env -> compose/url/suffix/data-default) — and drives the existing Dokploy call layer
(update the compose env to the digest, then deploy). One primitive, three configs
(preview/staging/prod); finance_report#883 P2 step 3.

This is the first piece with side effects: it mutates a Dokploy compose's env and
triggers a deploy. The Dokploy client is injected so it is unit-testable without a live
control plane. Step 4a adds an opt-in rollout wait (wait=True) and a CLI entry so a
workflow can drive it directly; the data-axis side effects (P3/#893) are still layered
on later. This owns assembly + trigger + the gates + (optionally) the rollout poll.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
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


def _dep_id(deployment: dict) -> str:
    return str(deployment.get("deploymentId") or deployment.get("id") or "")


def _deployment_ids(deployments) -> set[str]:
    return {_dep_id(d) for d in (deployments or []) if _dep_id(d)}


def wait_for_rollout(
    client,
    compose_id: str,
    before_ids: set[str],
    *,
    timeout: int = 600,
    interval: int = 5,
    _sleep=time.sleep,
    _now=time.monotonic,
) -> dict:
    """Poll until a NEW Dokploy deployment record reaches a terminal-good status.

    Raises RuntimeError if the new record errors, TimeoutError if none finishes in the
    window. Mirrors libs.deployer._wait_for_new_deployment_record — P2 step 5 collapses
    the two duplicate rollout pollers (D5) into one once deploy logic lands in infra2.
    """
    deadline = _now() + max(0, timeout)
    while True:
        new = [
            d
            for d in (client.get_compose_deployments(compose_id) or [])
            if _dep_id(d) and _dep_id(d) not in before_ids
        ]
        for d in new:
            status = str(d.get("status") or "").lower()
            if status == "error":
                raise RuntimeError(
                    f"deploy rollout entered error (compose {compose_id})"
                )
            if status in {"done", "success", "successful"}:
                return d
        if _now() >= deadline:
            raise TimeoutError(
                f"deploy rollout did not finish within {timeout}s (compose {compose_id})"
            )
        _sleep(max(1, interval))


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
    wait: bool = False,
    timeout: int = 600,
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

    When wait=True, block until a new Dokploy deployment record for this compose reaches
    a terminal-good status (raising on rollout error / timeout) — the readiness gate the
    App-repo bash primitive used to own. Default False keeps the pure-assembly callers
    (and the unit tests) side-effect-free beyond the env-update + deploy trigger.
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
    # Snapshot deployment ids BEFORE triggering so wait_for_rollout watches the new one.
    before_ids = (
        _deployment_ids(client.get_compose_deployments(cfg.compose_id)) if wait else set()
    )
    client.update_compose_env(cfg.compose_id, env_vars=env_vars)
    client.deploy_compose(cfg.compose_id)
    if wait:
        wait_for_rollout(client, cfg.compose_id, before_ids, timeout=timeout)

    return DeployPlan(
        env=env, sha=sha, compose_id=cfg.compose_id, data=data, env_vars=env_vars
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry so a workflow can call the primitive directly:

        python -m tools.deploy_primitive --env staging --code main --domain zitian.party

    Resolves code->sha, assembles the env, triggers the Dokploy deploy, and (unless
    --no-wait) blocks on rollout. This is the single deploy seam P2 step 4b-d switches
    the App-repo staging/prod/preview callers onto.
    """
    parser = argparse.ArgumentParser(description="commit-addressed deploy primitive")
    parser.add_argument("--env", required=True, help="staging | prod (not preview)")
    parser.add_argument("--code", required=True, help="main | release/x.y | vX.Y.Z | <sha>")
    parser.add_argument("--domain", required=True, help="base domain, e.g. zitian.party")
    parser.add_argument("--data", default=None, help="override the env's default data source")
    parser.add_argument("--repo", default=None, help="git remote to resolve code against")
    parser.add_argument(
        "--staging-validated",
        action="store_true",
        help="assert this exact digest passed staging (required for prod)",
    )
    parser.add_argument(
        "--break-glass",
        action="store_true",
        help="audited override of the staging-first gate (H5)",
    )
    parser.add_argument("--no-wait", action="store_true", help="do not block on rollout")
    parser.add_argument("--timeout", type=int, default=600, help="rollout wait seconds")
    args = parser.parse_args(argv)

    # Imported lazily so importing the primitive (and its unit tests) needs no Dokploy creds.
    from libs.dokploy import get_dokploy

    try:
        plan = deploy(
            args.env,
            args.code,
            domain=args.domain,
            client=get_dokploy(),
            data=args.data,
            staging_validated=args.staging_validated,
            break_glass=args.break_glass,
            repo=args.repo,
            wait=not args.no_wait,
            timeout=args.timeout,
        )
    except (ValueError, RuntimeError, TimeoutError) as exc:
        print(f"deploy failed: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "env": plan.env,
                "sha": plan.sha,
                "compose_id": plan.compose_id,
                "data": plan.data,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
