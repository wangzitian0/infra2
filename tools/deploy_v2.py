#!/usr/bin/env python3
"""Unified deploy front door: resolve the coordinate, validate, then dispatch.

``deploy_v2(...)`` is the single entrypoint for the deploy_v2 coordinate
``(service, type, version_ref, iac_ref)``. ``type`` is the discriminant: it interprets
``version_ref`` (PR# / sha / tag / branch -> a resolved sha + the image_ref to pull),
fails closed on a form it does not accept, derives the env + sub_domain, and declares its
gates. It builds + validates the :class:`~tools.deploy_contract.DeployTarget` (so no
illegal target reaches a backend), enforces the gates + data-lane red lines, then routes
to the existing, already-tested backend — passing the resolved ``image_ref``:

    type -> preview/*    -> preview_lifecycle.up   (iac_ref pins the GitHub source ref)
    type -> staging|prod -> deploy_primitive.deploy (the fixed-compose promote path)

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
    DeployTarget,
    deploy_type_spec,
    make_target,
    service_spec,
    validate_deploy_target,
    validate_ref_form,
)
from tools.deploy_env_config import env_config
from tools.deploy_primitive import deploy as _deploy_fixed
from tools.preview_lifecycle import up as _preview_up
from tools.resolve_deploy_ref import (
    classify_ref,
    resolve_image_ref,
    resolve_pr,
    resolve_to_sha,
)

_APP_SERVICE = "finance_report/app"
_APP_REPO = "https://github.com/wangzitian0/finance_report.git"
_INFRA2_REPO = "https://github.com/wangzitian0/infra2"
# Dokploy's github source clones a branch/tag ref (`git clone -b`), NOT a commit sha — a
# raw sha fails "Remote branch <sha> not found" (finance_report#342). So when iac_ref is a
# sha we clone the default branch; a branch/tag iac_ref is cloned verbatim (this is what
# dissolves the old separate `iac_branch` input — the iac_ref surface now drives the clone).
_INFRA2_DEFAULT_BRANCH = "main"
# The canary runs arbitrary code on a fixed throwaway preview slot no real PR reuses.
_CANARY_PR = 999


def _resolve_for_type(spec, version_ref, *, repo: str):
    """Resolve a type's ``version_ref`` surface to ``(ResolvedRef, alias_value)``.

    The ``type`` decides how ``version_ref`` is read (a discriminated union), and the
    matrix (``accepted_forms``) fails closed on a form the type does not take:

    - ``canary``         — any code form; runs on the fixed ``pr-<_CANARY_PR>`` slot.
    - ``preview/pr``     — ``version_ref`` IS a PR number (``resolve_pr`` -> PR-head image);
                           its slot is that number.
    - everything else    — ``version_ref`` is a git ref: validate its form against the type,
                           then ``resolve_image_ref``. The preview slot value is the tag
                           (``preview/tag``), the short sha (``preview/commit``), or absent
                           (``preview/main`` / fixed staging+prod).
    """
    if spec.key == "canary":
        ref = (str(version_ref).strip() if version_ref is not None else "") or "main"
        validate_ref_form(spec.key, classify_ref(ref))
        return resolve_image_ref(ref, repo=repo), _CANARY_PR
    if spec.alias_kind == "pr":
        return resolve_pr(version_ref, repo=repo), version_ref
    ref = str(version_ref).strip()
    validate_ref_form(spec.key, classify_ref(ref))
    resolved = resolve_image_ref(ref, repo=repo)
    alias_value = {
        "main": None,
        "commit": resolved.sha,  # preview_alias truncates to the 7-char short sha
        "tag": ref,
        None: None,  # fixed staging / prod carry no preview slot
    }[spec.alias_kind]
    return resolved, alias_value


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
    deploy_type: str,
    version_ref,
    iac_ref: str,
    client,
    domain: str,
    wait: bool = True,
    staging_validated: bool = False,
    break_glass: bool = False,
    code_reviewed: bool | None = None,
    timeout: int = 600,
    repo: str = _APP_REPO,
) -> DeployV2Result:
    """Execute one deploy_v2 coordinate ``(service, type, version_ref, iac_ref)``.

    The ``type`` is the discriminant: it interprets ``version_ref`` (a PR# / sha / tag /
    branch — :func:`_resolve_for_type`), fails closed on a form it does not accept, and
    declares its gates. ``version_ref`` resolves to the commit identity (``sha``) AND the
    published ``image_ref`` the backend pulls (a short sha for code, a retained tag for a
    release). ``iac_ref`` (a branch/tag/sha of infra2) pins the IaC and, when cloneable,
    the preview compose template.

    Raises ``ValueError`` for any contract / form / gate / red-line / unsupported-service
    violation BEFORE any side effect; backend errors (rollout / health) propagate.
    """
    spec = deploy_type_spec(deploy_type)
    resolved, alias_value = _resolve_for_type(spec, version_ref, repo=repo)

    # iac_ref: the recorded identity is its sha; the preview clone uses the ref verbatim
    # when it is cloneable (branch/tag), else the default branch (a sha can't be cloned, #342).
    iac_form = classify_ref(iac_ref)
    iac_sha = resolve_to_sha(iac_ref, repo=_INFRA2_REPO)
    clone_ref = _INFRA2_DEFAULT_BRANCH if iac_form == "sha" else iac_ref.strip()

    target = make_target(
        deploy_type,
        service=service,
        version=resolved.sha,
        iac_ref=iac_sha,
        alias_value=alias_value,
    )
    validate_deploy_target(target, service_spec(service))  # defensive re-check
    data_lane = enforce_data_lane_red_lines(target, code_reviewed=code_reviewed)

    # Gate: prod promotes code already validated on staging — declared by the type, checked
    # here (break_glass is the audited emergency bypass).
    if spec.requires_staging_first and not (staging_validated or break_glass):
        raise ValueError(
            f"deploy type {deploy_type!r} requires a prior staging deploy "
            "(pass staging_validated, or break_glass for an emergency)"
        )

    if service != _APP_SERVICE:
        raise ValueError(
            f"{service!r} cannot deploy via this front door yet; only {_APP_SERVICE} "
            "is wired (platform services join when the deployer path is unified)."
        )

    if env_config(target.env).dynamic:  # preview (incl. canary)
        result = _preview_up(
            spec.alias_kind,
            alias_value,
            code=resolved.sha,
            image_ref=resolved.image_ref,
            domain=domain,
            client=client,
            branch=clone_ref,
            wait=wait,
            health_timeout=timeout,
        )
        detail = {
            "alias": result.alias,
            "compose_id": result.compose_id,
            "sha": result.sha,
            "image_ref": resolved.image_ref,
            "url": result.url,
            "healthy": result.healthy,
        }
        return DeployV2Result(target, data_lane, "preview-lifecycle", detail)

    # staging | prod: the fixed-compose promote path. The backend pulls image_ref (a tag
    # for a release, the short sha for code); iac_ref is carried on the target for the record.
    plan = _deploy_fixed(
        target.env,
        resolved.sha,
        domain=domain,
        client=client,
        image_ref=resolved.image_ref,
        wait=wait,
        timeout=timeout,
        staging_validated=staging_validated,
        break_glass=break_glass,
    )
    detail = {
        "env": plan.env,
        "sha": plan.sha,
        "image_ref": resolved.image_ref,
        "compose_id": plan.compose_id,
        "data": plan.data,
        "iac_ref": target.iac_ref,
    }
    return DeployV2Result(target, data_lane, "deploy-primitive", detail)


def main(argv: list[str] | None = None) -> int:
    """CLI entry for the unified front door — the seam a deploy workflow invokes.

    Builds the Dokploy client, runs ``deploy_v2`` (which resolves the version_ref/iac_ref
    surfaces itself), and prints the result as one JSON line. This is the importable handle
    the App-repo deploy workflows route through during the cutover (finance_report#883),
    replacing the per-env bash. Dormant until a workflow calls it; no caller is wired here.
    """
    parser = argparse.ArgumentParser(description="unified deploy_v2 front door")
    parser.add_argument("--service", default=_APP_SERVICE, help="service key")
    parser.add_argument(
        "--type",
        required=True,
        dest="deploy_type",
        help="deploy type: staging | prod | preview/main | preview/pr | preview/commit "
        "| preview/tag | canary",
    )
    parser.add_argument(
        "--version-ref",
        required=True,
        help="version surface, interpreted by --type: a PR# (preview/pr), a release tag "
        "vX.Y.Z (prod / preview/tag), a sha (preview/commit), main, or release/x.y",
    )
    parser.add_argument(
        "--iac-ref",
        required=True,
        help="infra2 ref pinning the IaC: main | release/x.y | vX.Y.Z | <sha>",
    )
    parser.add_argument(
        "--domain", required=True, help="base domain, e.g. zitian.party"
    )
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

    # Imported lazily so importing the module (and its unit tests) needs no Dokploy creds.
    from libs.dokploy import get_dokploy

    client = get_dokploy(host=f"cloud.{args.domain}")
    try:
        result = deploy_v2(
            service=args.service,
            deploy_type=args.deploy_type,
            version_ref=args.version_ref,
            iac_ref=args.iac_ref,
            client=client,
            domain=args.domain,
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
