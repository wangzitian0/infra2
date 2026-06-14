#!/usr/bin/env python3
"""Unified deploy front door: validate the 5-axis contract, then dispatch.

``deploy_v2(...)`` is the single entrypoint for the deploy_v2 coordinate
``(service, env, sub_domain, code_version, iac_ref)``. It builds + validates the
:class:`~tools.deploy_contract.DeployTarget` (so no illegal target reaches a backend),
enforces the data-lane red lines, then routes to the existing, already-tested backend:

    env = preview        -> preview_lifecycle.up   (iac_ref pins the GitHub source ref)
    env = staging | prod -> deploy_primitive.deploy (the fixed-compose promote path)

Scope today: the finance_report **app** service — the only service these two primitives
deploy. Platform services (via ``libs/deployer`` + the iac_runner ``/deploy`` webhook)
join this front door when that second deploy path is unified; see
``docs/ssot/core.environments.md`` §4.7 "现状边界". This module performs the routing only
— the side effects live in the backends, so it is exercised with monkeypatched backends
(no live Dokploy).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass

import httpx  # Dokploy transport errors from libs.dokploy surface as httpx exceptions

from tools.deploy_contract import (
    _SHA_RE,
    DeployTarget,
    make_deploy_target,
    service_spec,
    validate_deploy_target,
)
from tools.deploy_env_config import env_config
from tools.deploy_primitive import deploy as _deploy_fixed
from tools.preview_lifecycle import up as _preview_up

_APP_SERVICE = "finance_report/app"
_INFRA2_REPO = "https://github.com/wangzitian0/infra2"


def resolve_data_lane(target: DeployTarget) -> str:
    """The data source for a target — derived from the env, not a separate input axis."""
    return env_config(target.env).data_default


def enforce_data_lane_red_lines(
    target: DeployTarget, *, code_reviewed: bool | None = None
) -> str:
    """Fail closed on the data red lines, returning the resolved data_lane.

    - ``env=prod`` must run on prod data (taxonomy consistency).
    - RL-DATA-1: code may touch prod data only with a *positive* review signal. This is
      deny-by-default: ``code_reviewed`` must be explicitly ``True`` for any prod-data
      target; the ``None`` default (signal absent) and ``False`` both fail closed, so a
      caller cannot reach prod data by simply omitting the argument. Full GitHub-review
      gating that supplies the ``True`` signal lands with the data axis
      (finance_report#893); until then prod-data deploys must pass it explicitly.
    """
    data_lane = resolve_data_lane(target)
    if target.env == "prod" and data_lane != "prod":
        raise ValueError(f"env=prod must use prod data, got data_lane={data_lane!r}")
    if data_lane == "prod" and code_reviewed is not True:
        raise ValueError(
            "prod data requires an explicit code-reviewed signal (RL-DATA-1); "
            f"got code_reviewed={code_reviewed!r}"
        )
    return data_lane


@dataclass(frozen=True)
class DeployV2Result:
    target: DeployTarget
    data_lane: str
    backend: str  # "preview-lifecycle" | "deploy-primitive"
    detail: dict


def deploy_v2(
    *,
    service: str,
    env: str,
    code_version: str,
    iac_ref: str,
    client,
    domain: str,
    alias_kind: str | None = None,
    alias_value=None,
    wait: bool = True,
    staging_validated: bool = False,
    break_glass: bool = False,
    code_reviewed: bool | None = None,
    timeout: int = 600,
) -> DeployV2Result:
    """Validate and execute a deploy_v2 target.

    For preview, pass ``alias_kind`` (main | pr | commit) and ``alias_value``. Raises
    ``ValueError`` for any contract / red-line / unsupported-service violation BEFORE any
    side effect; backend errors (rollout / health) propagate from the backend.
    """
    target = make_deploy_target(
        service=service,
        env=env,
        code_version=code_version,
        iac_ref=iac_ref,
        alias_kind=alias_kind,
        alias_value=alias_value,
    )
    validate_deploy_target(target, service_spec(service))  # defensive re-check
    data_lane = enforce_data_lane_red_lines(target, code_reviewed=code_reviewed)

    if service != _APP_SERVICE:
        raise ValueError(
            f"{service!r} cannot deploy via this front door yet; only {_APP_SERVICE} "
            "is wired (platform services join when the deployer path is unified)."
        )

    if env_config(target.env).dynamic:  # preview
        # iac_ref pins the infra2 ref Dokploy pulls the preview compose template from.
        result = _preview_up(
            alias_kind,
            alias_value,
            code=target.code_version,
            domain=domain,
            client=client,
            branch=target.iac_ref,
            wait=wait,
            health_timeout=timeout,
        )
        detail = {
            "alias": result.alias,
            "compose_id": result.compose_id,
            "sha": result.sha,
            "url": result.url,
            "healthy": result.healthy,
        }
        return DeployV2Result(target, data_lane, "preview-lifecycle", detail)

    # staging | prod: the fixed-compose promote path. iac_ref is carried on the target
    # for the record; source-ref pinning of the fixed composes is the remaining seam
    # (preview already pins it via the GitHub source branch above).
    plan = _deploy_fixed(
        target.env,
        target.code_version,
        domain=domain,
        client=client,
        wait=wait,
        timeout=timeout,
        staging_validated=staging_validated,
        break_glass=break_glass,
    )
    detail = {
        "env": plan.env,
        "sha": plan.sha,
        "compose_id": plan.compose_id,
        "data": plan.data,
        "iac_ref": target.iac_ref,
    }
    return DeployV2Result(target, data_lane, "deploy-primitive", detail)


def _resolve_refs(code: str, iac_ref: str) -> tuple[str, str]:
    """Resolve the surface inputs to 40-hex shas (code vs app repo, iac_ref vs infra2).

    Raises ``ValueError`` if either resolves to something that is not a full 40-hex sha,
    so the CLI fails fast here rather than late inside the contract validation.
    """
    from tools.resolve_deploy_ref import resolve_to_sha

    code_sha = resolve_to_sha(code)
    iac_sha = resolve_to_sha(iac_ref, repo=_INFRA2_REPO)
    for label, value in (("--code", code_sha), ("--iac-ref", iac_sha)):
        if not _SHA_RE.match(value):
            raise ValueError(
                f"{label} resolved to {value!r}, not a full 40-hex commit sha; "
                "pass a branch/tag or a full sha"
            )
    return code_sha, iac_sha


def main(argv: list[str] | None = None) -> int:
    """CLI entry for the unified front door — the seam a deploy workflow invokes.

    Resolves the surface refs, builds the Dokploy client, runs ``deploy_v2``, and prints
    the result as one JSON line. This is the importable handle the App-repo deploy
    workflows route through during the cutover (finance_report#883), replacing the
    per-env bash. Dormant until a workflow calls it; no caller is wired here.
    """
    parser = argparse.ArgumentParser(description="unified deploy_v2 front door")
    parser.add_argument("--service", default=_APP_SERVICE, help="service key")
    parser.add_argument("--env", required=True, choices=["preview", "staging", "prod"])
    parser.add_argument(
        "--code", required=True, help="app code: a branch/tag (e.g. main) or 40-hex sha"
    )
    parser.add_argument(
        "--iac-ref", required=True, help="infra2 ref: a branch/tag or 40-hex sha"
    )
    parser.add_argument(
        "--domain", required=True, help="base domain, e.g. zitian.party"
    )
    parser.add_argument(
        "--alias-kind", choices=["main", "pr", "commit"], help="preview alias kind"
    )
    parser.add_argument("--alias-value", default=None, help="preview alias value")
    parser.add_argument("--no-wait", action="store_true", help="do not health-check")
    parser.add_argument(
        "--staging-validated",
        action="store_true",
        help="assert this code already passed staging (required for prod)",
    )
    parser.add_argument(
        "--break-glass", action="store_true", help="bypass staging-first (emergency)"
    )
    parser.add_argument(
        "--code-reviewed",
        action="store_true",
        help="positive RL-DATA-1 signal — required for any prod-data deploy",
    )
    parser.add_argument("--timeout", type=int, default=600, help="health-check seconds")
    args = parser.parse_args(argv)

    try:
        code_sha, iac_sha = _resolve_refs(args.code, args.iac_ref)
    except (ValueError, RuntimeError) as exc:
        print(f"deploy_v2 ref resolution failed: {exc}", file=sys.stderr)
        return 2

    # Imported lazily so importing the module (and its unit tests) needs no Dokploy creds.
    from libs.dokploy import get_dokploy

    client = get_dokploy(host=f"cloud.{args.domain}")
    try:
        result = deploy_v2(
            service=args.service,
            env=args.env,
            code_version=code_sha,
            iac_ref=iac_sha,
            client=client,
            domain=args.domain,
            alias_kind=args.alias_kind,
            alias_value=args.alias_value,
            wait=not args.no_wait,
            staging_validated=args.staging_validated,
            break_glass=args.break_glass,
            # store_true yields False when omitted; pass True only when explicitly set so
            # RL-DATA-1 stays deny-by-default for prod data.
            code_reviewed=True if args.code_reviewed else None,
            timeout=args.timeout,
        )
    except (ValueError, RuntimeError, TimeoutError, httpx.HTTPError) as exc:
        print(f"deploy_v2 failed: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "service": result.target.service,
                "env": result.target.env,
                "sub_domain": result.target.sub_domain,
                "data_lane": result.data_lane,
                "backend": result.backend,
                "detail": result.detail,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
