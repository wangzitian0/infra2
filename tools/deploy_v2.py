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

from dataclasses import dataclass

from tools.deploy_contract import (
    DeployTarget,
    make_deploy_target,
    service_spec,
    validate_deploy_target,
)
from tools.deploy_env_config import env_config
from tools.deploy_primitive import deploy as _deploy_fixed
from tools.preview_lifecycle import up as _preview_up

_APP_SERVICE = "finance_report/app"


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
