#!/usr/bin/env python3
"""Unified deploy front door: resolve the coordinate, validate, then dispatch.

``deploy_v2(...)`` is the single entrypoint for the deploy_v2 coordinate
``(service, type, version_ref, iac_ref)``. ``type`` is the discriminant: it interprets
``version_ref`` (PR# / sha / tag / branch -> a resolved sha + the image_ref to pull),
fails closed on a form it does not accept, derives the env + sub_domain, and declares its
gates. It builds + validates the :class:`~libs.deploy_contract.DeployTarget` (so no
illegal target reaches a backend), enforces the gates + data-lane red lines, then routes
to the existing, already-tested backend — passing the resolved ``image_ref``:

    app + preview/*       -> libs.deploy.preview.up   (iac_ref pins the source ref)
    app + staging|prod    -> libs.deploy.promote.deploy (fixed-compose promote path)
    iac_pinned + fixed env -> iac_runner /deploy webhook (platform/backing services)

This module performs the routing only. Side effects live in the backends, so tests
exercise it with monkeypatched Dokploy/iac_runner clients rather than live control-plane
calls.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx  # Dokploy transport errors from libs.dokploy surface as httpx exceptions

from libs.common import infra_domain
from libs.iac_runner_client import (
    STATUS_POLL_BACKOFF,
    STATUS_POLL_INITIAL_SECONDS,
    STATUS_POLL_MAX_SECONDS,
    poll_platform_deploy_status,
    status_poll_attempts,
    trigger_platform_deploy,
)
from libs.deploy_contract import (
    _SHA_RE,
    DeployTarget,
    DeployTypeSpec,
    ServiceSpec,
    deploy_type_spec,
    make_deploy_target,
    make_target,
    service_spec,
    validate_deploy_target,
    validate_iac_ref_form,
    validate_ref_form,
)
from libs.deploy.preflight import (
    _image_manifest_exists,
    _wait_for_image_dependencies,
    assert_iac_ref_on_main,
    enforce_data_lane_red_lines,
    resolve_data_lane,
)
from libs.deploy_env_config import CANARY_SLOT, env_config
from libs.core.registry import domain_for_service
from libs.deploy.promote import deploy as _deploy_fixed
from libs.deploy.promote import model_overrides_from_env
from libs.deploy.preview import _validate_domain
from libs.deploy.preview import down as _preview_down
from libs.deploy.preview import up as _preview_up
from tools.resolve_deploy_ref import (
    ResolvedRef,
    classify_ref,
    resolve_branch_to_sha,
    resolve_image_ref,
    resolve_pr,
    resolve_to_sha,
)

from libs.core.constants import APP_SOURCES

_APP_SERVICE = "finance_report/app"
_SERVICE_REPOS: dict[str, str] = {
    service: f"https://github.com/{repo}.git" for service, repo in APP_SOURCES.items()
}
_APP_REPO = _SERVICE_REPOS.get(
    _APP_SERVICE, "https://github.com/wangzitian0/finance_report.git"
)
_INFRA2_REPO = "https://github.com/wangzitian0/infra2"
# Dokploy's github source clones a branch/tag ref (`git clone -b`), NOT a commit sha — a
# raw sha fails "Remote branch <sha> not found" (finance_report#342). So when iac_ref is a
# sha we clone the default branch; a branch/tag iac_ref is cloned verbatim (this is what
# dissolves the old separate `iac_branch` input — the iac_ref surface now drives the clone).
_INFRA2_DEFAULT_BRANCH = "main"
__all__ = [
    "DeployV2Result",
    "Path",
    "_image_manifest_exists",
    "assert_iac_ref_on_main",
    "deploy_v2",
    "enforce_data_lane_red_lines",
    "main",
    "resolve_data_lane",
]


def _default_main(version_ref) -> str:
    """A ``branch`` / ``canary`` version_ref defaults to the main tip when omitted."""
    return (str(version_ref).strip() if version_ref is not None else "") or "main"


def _repo_for_service(service: str) -> str:
    """The git repo a service's ``version_ref`` (tag/sha/branch) resolves against."""
    return _SERVICE_REPOS.get(service, _APP_REPO)


def _resolve_for_type(spec, version_ref, *, repo: str):
    """Resolve a type's ``version_ref`` surface to ``(ResolvedRef, alias_value)``.

    The ``type`` decides how ``version_ref`` is read (a discriminated union), and the
    matrix (``accepted_forms``) fails closed on a form the type does not take:

    - ``canary``          — any ref form, code OR release (default main); runs on the
                            fixed ``pr-<CANARY_PR>`` slot (it is a deploy-path probe, so
                            it stays maximally flexible).
    - ``preview/pr``      — ``version_ref`` IS a PR number (``resolve_pr`` -> PR-head image);
                            its slot is that number.
    - ``preview/branch``  — a branch tip (default main); slot ``branch-<name>``.
    - everything else     — ``version_ref`` is a git ref: validate its form against the
                            type, then ``resolve_image_ref``. The slot is the tag
                            (``preview/tag``), the short sha (``preview/commit``), or absent
                            (fixed staging+prod).
    """
    if spec.key == "canary":
        ref = _default_main(version_ref)
        validate_ref_form(spec.key, classify_ref(ref))
        return resolve_image_ref(ref, repo=repo), CANARY_SLOT
    if spec.alias_kind == "pr":
        return resolve_pr(version_ref, repo=repo), version_ref
    # `branch` defaults to the main tip; the other ref types require an explicit version_ref.
    ref = (
        _default_main(version_ref)
        if spec.alias_kind == "branch"
        else str(version_ref).strip()
    )
    validate_ref_form(spec.key, classify_ref(ref))
    resolved = resolve_image_ref(ref, repo=repo)
    # A bare short sha resolves to itself (not a 40-hex commit) — reject with a surface-level
    # message instead of letting it surface late as an opaque code_version contract error.
    if not _SHA_RE.match(resolved.sha):
        raise ValueError(
            f"version_ref {version_ref!r} resolved to {resolved.sha!r}, not a full commit "
            "sha — pass main, a tag vX.Y.Z, or a full 40-hex sha"
        )
    alias_value = {
        "branch": ref,  # the branch name -> slot branch-<name>
        "commit": resolved.sha,  # preview_alias truncates to the 7-char short sha
        "tag": ref,
        None: None,  # fixed staging / prod carry no preview slot
    }[spec.alias_kind]
    return resolved, alias_value


def _normalize_expected_sha(expected_sha: str | None) -> str | None:
    """Return a lowercase full expected commit sha, or fail before side effects."""
    if expected_sha is None:
        return None
    cleaned = str(expected_sha).strip().lower()
    if not cleaned:
        return None
    if not _SHA_RE.match(cleaned):
        raise ValueError("--expected-sha must be a full 40-hex commit sha")
    return cleaned


@dataclass(frozen=True)
class DeployV2Result:
    target: DeployTarget
    data_lane: str
    backend: str  # "preview-lifecycle" | "deploy-primitive" | "iac-runner"
    detail: dict


def _deploy_platform(
    service: str,
    spec,
    deploy_type: str,
    iac_ref: str,
    *,
    runner_url: str | None,
    secret: str | None,
    triggered_by: str,
    code_reviewed: bool | None,
    wait: bool,
    timeout: int,
    version_ref: str | None = None,
) -> DeployV2Result:
    """Route a platform (iac_pinned) service to the iac_runner ``/deploy`` webhook.

    We do NOT re-implement the platform deploy — ``Deployer.sync`` is Context/os.environ
    coupled — we trigger the SAME signed webhook ``deploy.yml`` uses, so the deploy
    is byte-for-byte iac_runner's. A platform artifact IS the ``iac_ref``-pinned stack, so
    the deploy ref (and the recorded version identity) is the resolved infra2 sha.
    ``version_ref`` is forwarded to the runner only when it names an APP release — a tag
    or sha that differs from the ``iac_ref`` — for a digest-pinned platform service such
    as ``truealpha/data_engine`` to pin (truealpha#712); ``main``, or a ref equal to the
    ``iac_ref`` (the reconcile's shape), is ignored. Platform services have no preview —
    only ``staging`` / ``prod``.
    """
    type_spec = deploy_type_spec(deploy_type)
    if type_spec.env not in ("staging", "prod"):
        raise ValueError(
            f"platform service {service!r} deploys to staging/prod only "
            f"(type {deploy_type!r} -> env {type_spec.env!r}); iac-pinned services have no preview"
        )
    iac_sha = resolve_to_sha(iac_ref, repo=_INFRA2_REPO)
    # The record: a platform service's version identity IS the infra2 commit (no app code).
    target = make_deploy_target(
        service=service, env=type_spec.env, code_version=iac_sha, iac_ref=iac_sha
    )
    validate_deploy_target(target, spec)  # enforces prod_only / env legality
    # RL-DATA-1 applies to platform prod too: a prod deploy must carry an explicit
    # code_reviewed signal (deny-by-default), same as the app path — a prod platform service
    # (e.g. postgres) sits on real prod data.
    data_lane = enforce_data_lane_red_lines(target, code_reviewed=code_reviewed)

    env = "production" if type_spec.env == "prod" else "staging"
    # iac_runner's SERVICE_TASK_MAP keys on the FULL service key (e.g. "platform/redis"),
    # NOT a shortname — pass the registry key verbatim or it skips as "no sync task configured".
    # We always FIRE the trigger with wait=False (the signed POST returns promptly, no 60s
    # timeout on a slow rollout); when the caller wants to wait we poll /deploy/status until
    # it settles — terminal success is "completed", failure is "failed" (verified live).
    url = runner_url or os.getenv("IAC_RUNNER_URL", "")
    sec = secret or os.getenv("IAC_WEBHOOK_SECRET", "")
    # An app release to pin (truealpha#712): forwarded only when it names one — a tag or
    # a sha, never the ``main`` default — so every other platform service sees nothing new.
    pinned_ref = (version_ref or "").strip()
    pinned_ref = pinned_ref if pinned_ref and pinned_ref != "main" else None
    if pinned_ref is not None and pinned_ref == iac_ref.strip():
        # The reconcile pins both axes to the infra2 release tag (tools/reconcile_iac_inputs:
        # "iac_pinned services ignore version_ref"); an infra2 tag is never an app release,
        # so it must not reach a digest-pinning deployer (v1.1.59 would have asked the
        # registry for truealpha-data-engine:v1.1.59). Only a ref that differs from the
        # iac_ref names an app release.
        pinned_ref = None
    started = time.monotonic()
    response = trigger_platform_deploy(
        env=env,
        ref=iac_sha,
        services=[service],
        base_url=url,
        secret=sec,
        triggered_by=triggered_by,
        wait=False,
        version_ref=pinned_ref,
    )
    detail = {"env": env, "ref": iac_sha, "services": [service], "iac_runner": response}
    if pinned_ref:
        detail["version_ref"] = pinned_ref
    if wait:
        final = poll_platform_deploy_status(
            env=env,
            ref=iac_sha,
            services=[service],
            deployment_id=response.get("deployment_id"),
            base_url=url,
            secret=sec,
            triggered_by=triggered_by,
            attempts=_poll_attempts_for_timeout(timeout),
            version_ref=pinned_ref,
            **_STATUS_POLL_SCHEDULE,
        )
        detail["iac_runner_final"] = final
        status = str(final.get("status", "")).lower()
        _progress(f"iac-runner sync of {service} to {env}", status, started)
        if status != "completed":  # "failed" / anything non-success
            raise RuntimeError(
                f"platform deploy of {service} to {env} ended {status!r}: "
                f"{final.get('details') or final}"
            )
    return DeployV2Result(target, data_lane, "iac-runner", detail)


# The /deploy/status schedule every deploy_v2 wait uses (truealpha#860): 2 s first, then
# growing to the 10 s it always was. See libs.iac_runner_client for the arithmetic.
_STATUS_POLL_SCHEDULE = {
    "interval": STATUS_POLL_INITIAL_SECONDS,
    "backoff": STATUS_POLL_BACKOFF,
    "max_interval": STATUS_POLL_MAX_SECONDS,
}


def _poll_attempts_for_timeout(timeout: int) -> int:
    """Convert a seconds budget into iac_runner status poll attempts on that schedule."""
    return status_poll_attempts(
        max(1, int(timeout)),
        initial=STATUS_POLL_INITIAL_SECONDS,
        backoff=STATUS_POLL_BACKOFF,
        maximum=STATUS_POLL_MAX_SECONDS,
    )


def _progress(what: str, outcome: str, started: float) -> None:
    """One stderr line per finished deploy phase, flushed as it happens.

    The receiver's step log timestamps each line when it arrives; before this, the
    execute step printed its result only at the very end and the time between a secret
    supply and the next sync could only be reconstructed from three other logs
    (truealpha#860). Worded so it never looks like the ``deploy_v2 failed:`` verdict line
    or a runner ``ended '<status>': {...}`` record that tools/reconcile_iac_inputs parses.
    """
    print(
        f"deploy_v2 progress: {what}: {outcome or 'unknown'} "
        f"after {time.monotonic() - started:.1f}s",
        file=sys.stderr,
        flush=True,
    )


def _supply_app_secrets(
    service: str,
    env: str,
    iac_sha: str,
    *,
    runner_url: str | None,
    secret: str | None,
    triggered_by: str,
    timeout: int,
) -> dict:
    """Run the deploy-time secret supply for an app stack through the iac-runner.

    App stacks promote through Dokploy directly (``_deploy_fixed``), a path that never
    enters their Deployer, so ``Deployer.apply_secret_supply`` (copy human values from
    1Password, generate runtime ones, report what is missing) would never run for them.
    The runner has the AppRole and the 1Password service account this needs; Actions has
    neither. So the same signed ``/deploy`` webhook is fired with ``action=secrets-supply``
    and awaited before the promote — fail closed on a store that still lacks a required
    value (#649). Without runner credentials (a caller outside deploy.yml) the step is
    skipped and says so; the daily reconcile still reports the drift.
    """
    url = runner_url or os.getenv("IAC_RUNNER_URL", "")
    sec = secret or os.getenv("IAC_WEBHOOK_SECRET", "")
    if not url or not sec:
        return {
            "status": "skipped",
            "reason": "no iac-runner credentials in this context",
        }
    runner_env = "production" if env == "prod" else env
    started = time.monotonic()
    response = trigger_platform_deploy(
        env=runner_env,
        ref=iac_sha,
        services=[service],
        base_url=url,
        secret=sec,
        triggered_by=triggered_by,
        wait=False,
        action="secrets-supply",
    )
    final = poll_platform_deploy_status(
        env=runner_env,
        ref=iac_sha,
        services=[service],
        deployment_id=response.get("deployment_id"),
        base_url=url,
        secret=sec,
        triggered_by=triggered_by,
        attempts=_poll_attempts_for_timeout(timeout),
        action="secrets-supply",
        **_STATUS_POLL_SCHEDULE,
    )
    status = str(final.get("status", "")).lower()
    _progress(f"secret supply for {service} in {runner_env}", status, started)
    if status != "completed":
        raise RuntimeError(
            f"secret supply for {service} in {runner_env} ended {status!r}: "
            f"{final.get('details') or final.get('error') or final}"
        )
    return {"status": "completed", "iac_runner": response, "iac_runner_final": final}


def _deploy_platform_batch(
    services: list[str],
    deploy_type: str,
    iac_ref: str,
    *,
    runner_url: str | None,
    secret: str | None,
    triggered_by: str,
    code_reviewed: bool | None,
    wait: bool,
    timeout: int,
) -> dict:
    """Route multiple iac_pinned services through one deploy_v2/iac_runner call.

    The normal public API remains single-service. This CLI helper exists for
    post-merge reconcile fan-out so a manifest-wide input change produces one
    terminal-status wait per environment, not one wait per service.
    """
    if not services:
        raise ValueError("at least one service is required")
    type_spec = deploy_type_spec(deploy_type)
    if type_spec.env not in ("staging", "prod"):
        raise ValueError(
            f"iac_pinned batch deploys to staging/prod only "
            f"(type {deploy_type!r} -> env {type_spec.env!r})"
        )
    iac_sha = resolve_to_sha(iac_ref, repo=_INFRA2_REPO)
    targets: list[DeployTarget] = []
    data_lanes: set[str] = set()
    for service in services:
        spec = service_spec(service)
        if not spec.iac_pinned:
            raise ValueError(
                f"{service!r} is not iac_pinned; batch deploy only supports "
                "platform/backing services"
            )
        target = make_deploy_target(
            service=service, env=type_spec.env, code_version=iac_sha, iac_ref=iac_sha
        )
        validate_deploy_target(target, spec)
        data_lanes.add(enforce_data_lane_red_lines(target, code_reviewed=code_reviewed))
        targets.append(target)

    env = "production" if type_spec.env == "prod" else "staging"
    url = runner_url or os.getenv("IAC_RUNNER_URL", "")
    sec = secret or os.getenv("IAC_WEBHOOK_SECRET", "")
    started = time.monotonic()
    response = trigger_platform_deploy(
        env=env,
        ref=iac_sha,
        services=services,
        base_url=url,
        secret=sec,
        triggered_by=triggered_by,
        wait=False,
    )
    detail = {"env": env, "ref": iac_sha, "services": services, "iac_runner": response}
    if wait:
        final = poll_platform_deploy_status(
            env=env,
            ref=iac_sha,
            services=services,
            deployment_id=response.get("deployment_id"),
            base_url=url,
            secret=sec,
            triggered_by=triggered_by,
            attempts=_poll_attempts_for_timeout(timeout),
            **_STATUS_POLL_SCHEDULE,
        )
        detail["iac_runner_final"] = final
        status = str(final.get("status", "")).lower()
        _progress(f"iac-runner sync of {', '.join(services)} to {env}", status, started)
        if status != "completed":
            raise RuntimeError(
                f"platform batch deploy of {services} to {env} ended {status!r}: "
                f"{final.get('details') or final}"
            )

    return {
        "service": services,
        "env": type_spec.env,
        "sub_domain": {target.service: target.sub_domain for target in targets},
        "data_lane": sorted(data_lanes),
        "backend": "iac-runner",
        "detail": detail,
    }


def _resolve_app_refs(
    service: str,
    spec: DeployTypeSpec,
    version_ref: str,
    iac_ref: str,
    iac_clone_ref: str | None,
    normalized_expected_sha: str | None,
    repo: str | None,
) -> tuple[ResolvedRef, str, str, str]:
    resolved_repo = repo if repo is not None else _repo_for_service(service)
    resolved, alias_value = _resolve_for_type(spec, version_ref, repo=resolved_repo)
    if (
        normalized_expected_sha is not None
        and resolved.sha.lower() != normalized_expected_sha
    ):
        raise ValueError(
            f"version_ref {version_ref!r} resolved to {resolved.sha!r}, "
            f"not expected sha {normalized_expected_sha!r}"
        )

    iac_form = classify_ref(iac_ref)
    iac_sha = resolve_to_sha(iac_ref, repo=_INFRA2_REPO)
    if iac_clone_ref is not None:
        clone_ref = iac_clone_ref.strip()
        if iac_form != "sha" or not clone_ref:
            raise ValueError(
                "--iac-clone-ref requires a non-empty clone ref and exact-SHA --iac-ref"
            )
        clone_sha = resolve_branch_to_sha(clone_ref, repo=_INFRA2_REPO)
        if clone_sha.lower() != iac_sha.lower():
            raise ValueError(
                f"iac clone ref {clone_ref!r} resolved to {clone_sha!r}, "
                f"not authoritative iac_ref SHA {iac_sha!r}"
            )
    else:
        clone_ref = _INFRA2_DEFAULT_BRANCH if iac_form == "sha" else iac_ref.strip()

    return resolved, alias_value, iac_sha, clone_ref


def _deploy_preview_v2(
    target: DeployTarget,
    svc_spec: ServiceSpec,
    spec: DeployTypeSpec,
    alias_value: str,
    service: str,
    resolved: ResolvedRef,
    domain: str,
    client: Any,
    clone_ref: str,
    wait: bool,
    timeout: int,
    data_lane: list[str],
) -> DeployV2Result:
    if not svc_spec.supports_preview:
        raise ValueError(
            f"{service!r} does not support preview/canary deploys yet "
            "(libs.deploy_contract.ServiceSpec.supports_preview=False) — register a "
            "libs.deploy_env_config.preview_service_config entry for it first (#522)."
        )
    result = _preview_up(
        spec.alias_kind,
        alias_value,
        code=resolved.sha,
        service=service,
        image_ref=resolved.image_ref,
        iac_ref=target.iac_ref,
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


def _deploy_fixed_v2(
    target: DeployTarget,
    resolved: ResolvedRef,
    service: str,
    domain: str,
    client: Any,
    clone_ref: str,
    wait: bool,
    timeout: int,
    staging_validated: bool,
    break_glass: bool,
    verify_vault: bool,
    verify_config: bool,
    verify_ingestion: bool,
    iac_runner_url: str | None,
    iac_webhook_secret: str | None,
    triggered_by: str,
    data_lane: list[str],
) -> DeployV2Result:
    secret_supply = _supply_app_secrets(
        service,
        target.env,
        target.iac_ref,
        runner_url=iac_runner_url,
        secret=iac_webhook_secret,
        triggered_by=triggered_by,
        timeout=timeout,
    )
    promote_started = time.monotonic()
    plan = _deploy_fixed(
        target.env,
        resolved.sha,
        domain=domain,
        client=client,
        service=service,
        image_ref=resolved.image_ref,
        iac_ref=target.iac_ref,
        branch=clone_ref,
        wait=wait,
        timeout=timeout,
        staging_validated=staging_validated,
        break_glass=break_glass,
        verify_vault=verify_vault,
        verify_config=verify_config,
        verify_ingestion=verify_ingestion,
        model_overrides=model_overrides_from_env(),
    )
    _progress(
        f"Dokploy promote of {service} to {target.env}",
        "in service" if wait else "triggered",
        promote_started,
    )
    detail = {
        "env": plan.env,
        "sha": plan.sha,
        "image_ref": resolved.image_ref,
        "compose_id": plan.compose_id,
        "data": plan.data,
        "iac_ref": target.iac_ref,
        "secret_supply": secret_supply,
        "rollback_class": plan.rollback_class,
    }
    return DeployV2Result(target, data_lane, "deploy-primitive", detail)


def deploy_v2(
    *,
    service: str,
    deploy_type: str,
    version_ref,
    iac_ref: str,
    iac_clone_ref: str | None = None,
    client,
    domain: str,
    wait: bool = True,
    staging_validated: bool = False,
    break_glass: bool = False,
    code_reviewed: bool | None = None,
    verify_vault: bool = True,
    verify_config: bool = True,
    verify_ingestion: bool = False,
    timeout: int = 600,
    expected_sha: str | None = None,
    repo: str | None = None,
    iac_runner_url: str | None = None,
    iac_webhook_secret: str | None = None,
    triggered_by: str = "deploy_v2",
    image_wait_seconds: float | None = None,
    image_poll_seconds: float | None = None,
) -> DeployV2Result:
    """Execute one deploy_v2 coordinate ``(service, type, version_ref, iac_ref)``."""
    domain = domain_for_service(service) or domain
    svc_spec = service_spec(service)
    normalized_expected_sha = _normalize_expected_sha(expected_sha)
    validate_iac_ref_form(deploy_type, classify_ref(iac_ref))
    spec = deploy_type_spec(deploy_type)
    if iac_clone_ref is not None and not env_config(spec.env).dynamic:
        raise ValueError("--iac-clone-ref is only supported for preview/canary deploys")
    assert_iac_ref_on_main(iac_ref, deploy_type)
    if svc_spec.iac_pinned:  # platform service -> iac_runner /deploy webhook
        if normalized_expected_sha is not None:
            raise ValueError("--expected-sha is only supported for app-backed deploys")
        return _deploy_platform(
            service,
            svc_spec,
            deploy_type,
            iac_ref,
            runner_url=iac_runner_url,
            secret=iac_webhook_secret,
            triggered_by=triggered_by,
            code_reviewed=code_reviewed,
            wait=wait,
            timeout=timeout,
            version_ref=version_ref,
        )

    resolved, alias_value, iac_sha, clone_ref = _resolve_app_refs(
        service,
        spec,
        version_ref,
        iac_ref,
        iac_clone_ref,
        normalized_expected_sha,
        repo,
    )
    target = make_target(
        deploy_type,
        service=service,
        version=resolved.sha,
        iac_ref=iac_sha,
        alias_value=alias_value,
    )
    validate_deploy_target(target, service_spec(service))
    data_lane = enforce_data_lane_red_lines(target, code_reviewed=code_reviewed)

    if env_config(spec.env).requires_staging_first and not (
        staging_validated or break_glass
    ):
        raise ValueError(
            f"deploy type {deploy_type!r} requires a prior staging deploy "
            "(pass staging_validated, or break_glass for an emergency)"
        )

    _wait_for_image_dependencies(
        svc_spec,
        resolved.image_ref,
        timeout=image_wait_seconds,
        poll_seconds=image_poll_seconds,
    )

    if env_config(target.env).dynamic:
        return _deploy_preview_v2(
            target,
            svc_spec,
            spec,
            alias_value,
            service,
            resolved,
            domain,
            client,
            clone_ref,
            wait,
            timeout,
            data_lane,
        )

    return _deploy_fixed_v2(
        target,
        resolved,
        service,
        domain,
        client,
        clone_ref,
        wait,
        timeout,
        staging_validated,
        break_glass,
        verify_vault,
        verify_config,
        verify_ingestion,
        iac_runner_url,
        iac_webhook_secret,
        triggered_by,
        data_lane,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="unified deploy_v2 front door")
    parser.add_argument("--service", default=_APP_SERVICE, help="service key")
    parser.add_argument(
        "--type",
        required=True,
        dest="deploy_type",
        help="deploy type: staging | prod | preview/branch | preview/pr | preview/commit "
        "| preview/tag | canary",
    )
    parser.add_argument(
        "--version-ref",
        default="",
        help="version surface, interpreted by --type: a PR# (preview/pr), a release tag "
        "vX.Y.Z (prod / preview/tag), a sha (preview/commit), or main; "
        "ignored for iac_pinned platform/backing services",
    )
    parser.add_argument(
        "--iac-ref",
        required=True,
        help="infra2 ref pinning the IaC: main | vX.Y.Z | <sha>",
    )
    parser.add_argument(
        "--domain", required=True, help="base domain, e.g. zitian.party"
    )
    parser.add_argument("--no-wait", action="store_true", help="do not health-check")
    parser.add_argument(
        "--down",
        action="store_true",
        help="tear down the preview/* alias selected by --type/--version-ref (and its "
        "ephemeral DB) instead of deploying; valid only for preview types",
    )
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
    parser.add_argument(
        "--skip-vault-check",
        action="store_true",
        help="skip the VAULT_APP_TOKEN TTL preflight (default: verify, fixed envs only)",
    )
    parser.add_argument(
        "--no-verify-config",
        action="store_true",
        help="skip the post-deploy effective IAC_CONFIG_HASH check (default: verify)",
    )
    parser.add_argument(
        "--verify-ingestion",
        action="store_true",
        help="after health, prove the deployed service.version ingests logs+traces into "
        "SigNoz (zero-ingestion vs stale-image); needs ClickHouse network access "
        "(default: off)",
    )
    parser.add_argument("--timeout", type=int, default=600, help="health-check seconds")
    parser.add_argument(
        "--expected-sha",
        default=None,
        help="optional full commit sha that version_ref must resolve to before deploy",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="git repo version_ref resolves against (default: per-service, see "
        "_SERVICE_REPOS)",
    )
    parser.add_argument(
        "--image-wait-seconds",
        type=float,
        default=None,
        help=(
            "seconds to wait for service image artifacts to publish "
            "(default: DEPLOY_V2_IMAGE_WAIT_SECONDS or 300)"
        ),
    )
    parser.add_argument(
        "--image-poll-seconds",
        type=float,
        default=None,
        help=(
            "seconds between image artifact readiness checks "
            "(default: DEPLOY_V2_IMAGE_POLL_SECONDS or 10)"
        ),
    )
    return parser


def _handle_teardown(args: argparse.Namespace) -> int:
    spec = deploy_type_spec(args.deploy_type)
    if spec.env != "preview":
        raise ValueError(
            f"--down only tears down preview/* aliases; type {args.deploy_type!r} "
            f"-> env {spec.env!r} has no ephemeral alias to remove"
        )
    if spec.alias_kind == "branch":
        alias_value = _default_main(args.version_ref)
    elif spec.alias_kind == "canary":
        alias_value = CANARY_SLOT
    else:
        alias_value = args.version_ref
    from libs.deploy.dokploy_client import get_dokploy

    domain = _validate_domain(args.domain)
    down_result = _preview_down(
        spec.alias_kind,
        alias_value,
        domain=domain,
        client=get_dokploy(host=f"cloud.{infra_domain()}"),
        service=args.service,
    )
    print(
        json.dumps(
            {
                "action": down_result.action,
                "alias": down_result.alias,
                "compose_id": down_result.compose_id,
                "url": down_result.url,
            }
        )
    )
    return 0


def _handle_platform_batch(args: argparse.Namespace, service_names: list[str]) -> int:
    result = _deploy_platform_batch(
        service_names,
        args.deploy_type,
        args.iac_ref,
        runner_url=os.getenv("IAC_RUNNER_URL", ""),
        secret=os.getenv("IAC_WEBHOOK_SECRET", ""),
        triggered_by="deploy_v2",
        code_reviewed=True if args.code_reviewed else None,
        wait=not args.no_wait,
        timeout=args.timeout,
    )
    print(json.dumps(result))
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry for the unified front door — the surface deploy workflows invoke."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    try:
        if args.down:
            return _handle_teardown(args)

        service_names = [s.strip() for s in args.service.split(",") if s.strip()]
        if len(service_names) > 1:
            return _handle_platform_batch(args, service_names)

        client = None
        if not service_spec(args.service).iac_pinned:
            from libs.deploy.dokploy_client import get_dokploy

            client = get_dokploy(host=f"cloud.{infra_domain()}")
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
            code_reviewed=True if args.code_reviewed else None,
            verify_vault=not args.skip_vault_check,
            verify_config=not args.no_verify_config,
            verify_ingestion=args.verify_ingestion,
            timeout=args.timeout,
            expected_sha=args.expected_sha,
            repo=args.repo,
            image_wait_seconds=args.image_wait_seconds,
            image_poll_seconds=args.image_poll_seconds,
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
