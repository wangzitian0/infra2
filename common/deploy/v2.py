#!/usr/bin/env python3
"""Unified deploy front door: resolve the coordinate, validate, then dispatch.

``deploy_v2(...)`` is the single entrypoint for the deploy_v2 coordinate
``(service, type, version_ref, iac_ref)``. ``type`` is the discriminant: it interprets
``version_ref`` (PR# / sha / tag / branch -> a resolved sha + the image_ref to pull),
fails closed on a form it does not accept, derives the env + sub_domain, and declares its
gates. It builds + validates the :class:`~common.deploy.contract.DeployTarget` (so no
illegal target reaches a backend), enforces the gates + data-lane red lines, then routes
to the existing, already-tested backend — passing the resolved ``image_ref``:

    app + preview/*       -> common.deploy.preview.up   (iac_ref pins the source ref)
    app + staging|prod    -> common.deploy.promote.deploy (fixed-compose promote path)
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
from typing import Any

import httpx  # Dokploy transport errors from common.deploy.dokploy_client surface as httpx exceptions

from common.core.environ import infra_domain
from common.deploy.iac_runner_client import (
    STATUS_POLL_BACKOFF,
    STATUS_POLL_INITIAL_SECONDS,
    STATUS_POLL_MAX_SECONDS,
    RunnerLostDeploymentError,
    poll_platform_deploy_status,
    status_poll_attempts,
    trigger_platform_deploy,
)
from common.core.constants import APP_SOURCES, REPO_ROOT
from common.deploy.contract import (
    DeployTarget,
    DeployTypeSpec,
    ServiceSpec,
    default_version_ref,
    deploy_type_spec,
    make_deploy_target,
    make_target,
    normalize_pr_number,
    resolve_deploy_type_for_ref,
    service_spec,
    validate_deploy_target,
    validate_iac_ref_form,
    validate_ref_form,
)
from common.deploy.release_markers import newest_release_tag
from common.deploy.preflight import (
    _image_manifest_exists,
    _wait_for_image_dependencies,
    assert_app_version_ref_matches_train,
    assert_app_version_ref_on_main,
    assert_iac_ref_on_main,
    enforce_data_lane_red_lines,
    resolve_data_lane,
)
from common.deploy.env_config import env_config, preview_service_config
from common.core.registry import domain_for_service
from common.deploy.app_contract import encode_rendered_templates, verify_app_manifests
from common.deploy.promote import PromoteDeps, PromoteOptions, deploy as _deploy_fixed
from common.deploy.preview import PreviewDeps, PreviewOptions, _validate_domain
from common.deploy.preview import down as _preview_down
from common.deploy.preview import up as _preview_up
from infra2_sdk.refs import (
    ResolvedRef,
    classify_ref,
    resolve_image_ref,
    resolve_pr,
    resolve_to_sha,
    short_sha,
)

_APP_SERVICE = "finance_report/app"
_SERVICE_REPOS: dict[str, str] = {
    service: f"https://github.com/{repo}.git" for service, repo in APP_SOURCES.items()
}
_APP_REPO = _SERVICE_REPOS.get(
    _APP_SERVICE, "https://github.com/wangzitian0/finance_report.git"
)
_INFRA2_REPO = "https://github.com/wangzitian0/infra2"
__all__ = [
    "DeployDeps",
    "DeployOptions",
    "DeployV2Result",
    "_image_manifest_exists",
    "assert_app_version_ref_matches_train",
    "assert_app_version_ref_on_main",
    "assert_iac_ref_on_main",
    "deploy_v2",
    "enforce_data_lane_red_lines",
    "main",
    "resolve_data_lane",
]


@dataclass(frozen=True)
class DeployOptions:
    """Optional policy gates, verification flags, and timeouts for ``deploy_v2``."""

    wait: bool = True
    timeout: int = 600
    staging_validated: bool = False
    code_reviewed: bool | None = None
    break_glass: bool = False
    bootstrap: bool = False
    force_promote: bool = False
    pause_rollback: bool = False
    max_rollback_attempts: int = 2
    verify_vault: bool = True
    verify_config: bool = True
    verify_smoke: bool = True
    verify_ingestion: bool = False
    verify_manifests: bool = False
    image_wait_seconds: float | None = None
    image_poll_seconds: float | None = None
    expected_sha: str | None = None
    triggered_by: str = "deploy_v2"


@dataclass(frozen=True)
class DeployDeps:
    """Optional injectable transport / runtime dependencies for ``deploy_v2``."""

    client: Any = None
    domain: str | None = None
    repo: str | None = None
    iac_runner_url: str | None = None
    iac_webhook_secret: str | None = None


def _default_main(version_ref) -> str:
    """A ``branch`` version_ref defaults to the main tip when omitted."""
    return (str(version_ref).strip() if version_ref is not None else "") or "main"


def _default_iac_ref(env: str) -> str:
    """Derive default ``iac_ref`` inside the control plane when callers omit it."""
    if env in ("staging", "prod"):
        return newest_release_tag(repo_root=REPO_ROOT)
    return "main"


def _repo_for_service(service: str) -> str:
    """The git repo a service's ``version_ref`` (tag/sha/branch) resolves against."""
    return _SERVICE_REPOS.get(service, _APP_REPO)


def _resolve_for_type(spec, version_ref, *, repo: str):
    """Resolve a type's ``version_ref`` surface to ``(ResolvedRef, alias_value)``.

    The ``type`` decides how ``version_ref`` is read (a discriminated union), and the
    matrix (``accepted_forms``) fails closed on a form the type does not take:

    - ``preview/pr``      — ``version_ref`` IS a PR ref (``pr-<N>`` or 1-6 digit int ->
                            ``resolve_pr`` -> PR-head image); its slot is that number.
    - ``preview/branch``  — a branch tip (default main); slot ``branch-<name>``.
    - ``staging`` / ``prod`` — ``version_ref`` is a release tag ``vX.Y.Z``: validate its form
                            against the type, then ``resolve_image_ref``.
    """
    if spec.alias_kind == "pr":
        pr_num = normalize_pr_number(version_ref)
        return resolve_pr(pr_num, repo=repo), pr_num
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
    try:
        short_sha(resolved.sha)
    except ValueError as err:
        raise ValueError(
            f"version_ref {version_ref!r} resolved to {resolved.sha!r}, not a full commit "
            "sha — pass main, a tag vX.Y.Z, or a full 40-hex sha"
        ) from err
    alias_value = {
        "branch": ref,  # the branch name -> slot branch-<name>
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
    try:
        short_sha(cleaned)
    except ValueError as err:
        raise ValueError("--expected-sha must be a full 40-hex commit sha") from err
    return cleaned


@dataclass(frozen=True)
class DeployV2Result:
    target: DeployTarget
    data_lane: str
    backend: str  # "preview-lifecycle" | "deploy-primitive" | "iac-runner"
    detail: dict


def _deploy_platform_preview(
    service: str,
    deploy_type: str,
    version_ref,
    iac_ref: str,
    *,
    client,
    domain: str,
    opt: DeployOptions,
) -> DeployV2Result:
    """Deploy a platform unit into its Preview slot through the one Preview lifecycle.

    The unit's Preview compose is rendered with throwaway secrets (``preview.up``), so the
    slot reads no Vault. ``version_ref`` names the slot only (a PR number or a branch);
    the code is the infra2 ``iac_ref``. Capability is registry membership.
    """
    preview_service_config(
        service
    )  # fails before side effects for an unregistered unit
    spec = resolve_deploy_type_for_ref(deploy_type, version_ref)
    alias_value = (
        _default_main(version_ref)
        if spec.alias_kind == "branch"
        else normalize_pr_number(version_ref)
    )
    iac_sha = resolve_to_sha(iac_ref, repo=_INFRA2_REPO)
    target = make_target(
        spec.key,
        service=service,
        version=iac_sha,
        iac_ref=iac_sha,
        alias_value=alias_value,
    )
    validate_deploy_target(target, service_spec(service))
    data_lane = enforce_data_lane_red_lines(target, code_reviewed=opt.code_reviewed)
    result = _preview_up(
        spec.alias_kind,
        alias_value,
        code=iac_sha,
        options=PreviewOptions(
            service=service,
            iac_ref=iac_sha,
            branch=iac_ref.strip(),
            wait=opt.wait,
            health_timeout=opt.timeout,
        ),
        deps=PreviewDeps(
            client=client,
            domain=domain,
            repo=_INFRA2_REPO,
        ),
    )
    detail = {
        "alias": result.alias,
        "compose_id": result.compose_id,
        "sha": result.sha,
        "url": result.url,
        "healthy": result.healthy,
    }
    return DeployV2Result(target, data_lane, "preview-lifecycle", detail)


def _deploy_platform(
    service: str,
    deploy_type: str,
    iac_ref: str,
    *,
    version_ref: str | None = None,
    options: DeployOptions | None = None,
    deps: DeployDeps | None = None,
) -> DeployV2Result:
    """Route a platform (iac_pinned) service to the iac_runner ``/deploy`` webhook.

    We do NOT re-implement the platform deploy — ``Deployer.sync`` is Context/os.environ
    coupled — we trigger the SAME signed webhook ``deploy.yml`` uses, so the deploy
    is byte-for-byte iac_runner's. A platform artifact IS the ``iac_ref``-pinned stack, so
    the deploy ref (and the recorded version identity) is the resolved infra2 sha.
    ``version_ref`` is forwarded to the runner only when it names an APP release — a tag
    or sha that differs from the ``iac_ref`` — for a digest-pinned platform service such
    as ``truealpha/data_engine`` to pin (truealpha#712); ``main``, or a ref equal to the
    ``iac_ref`` (the reconcile's shape), is ignored. This path serves ``staging`` / ``prod``
    only; a platform Preview goes to ``_deploy_platform_preview``.
    """
    opt = options or DeployOptions()
    dep = deps or DeployDeps()
    spec = service_spec(service)
    type_spec = deploy_type_spec(deploy_type)
    if type_spec.env not in ("staging", "prod"):
        raise ValueError(
            f"platform service {service!r} deploys to staging/prod only "
            f"(type {deploy_type!r} -> env {type_spec.env!r})"
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
    data_lane = enforce_data_lane_red_lines(target, code_reviewed=opt.code_reviewed)

    env = "production" if type_spec.env == "prod" else "staging"
    # iac_runner's SERVICE_TASK_MAP keys on the FULL service key (e.g. "platform/redis"),
    # NOT a shortname — pass the registry key verbatim or it skips as "no sync task configured".
    # We always FIRE the trigger with wait=False (the signed POST returns promptly, no 60s
    # timeout on a slow rollout); when the caller wants to wait we poll /deploy/status until
    # it settles — terminal success is "completed", failure is "failed" (verified live).
    url = dep.iac_runner_url or os.getenv("IAC_RUNNER_URL", "")
    sec = dep.iac_webhook_secret or os.getenv("IAC_WEBHOOK_SECRET", "")
    # Only services whose artifact comes from an App repo (e.g. truealpha/data_engine)
    # forward DEPLOY_VERSION_REF to pin an app image release. Pure infra2 services never do.
    pinned_ref: str | None = None
    ver_clean = (version_ref or "").strip()
    if spec.artifact_source == "app" and ver_clean:
        validate_ref_form(type_spec.key, classify_ref(ver_clean))
        pinned_ref = ver_clean
    started = time.monotonic()
    response, final = _trigger_and_poll(
        env=env,
        ref=iac_sha,
        services=[service],
        url=url,
        secret=sec,
        triggered_by=opt.triggered_by,
        wait=opt.wait,
        timeout=opt.timeout,
        started=started,
        version_ref=pinned_ref,
    )
    detail = {"env": env, "ref": iac_sha, "services": [service], "iac_runner": response}
    if pinned_ref:
        detail["version_ref"] = pinned_ref
    if opt.wait:
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
# growing to the 10 s it always was. See common.deploy.iac_runner_client for the arithmetic.
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
    # A production run shows each finished phase on its release card. The update has a
    # time bound and never stops the deploy.
    try:
        from common.alerting import release_card

        release_card.note_progress(os.environ, what, outcome)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[deploy_v2] release card update failed: {exc}",
            file=sys.stderr,
            flush=True,
        )


# A staging deploy re-submits a request the runner lost once; a production deploy never does
# (#666). Two submissions in all: the first, and the one re-submit.
_STAGING_SUBMISSIONS = 2


def _trigger_and_poll(
    *,
    env: str,
    ref: str,
    services: list[str],
    url: str,
    secret: str,
    triggered_by: str,
    wait: bool,
    timeout: int,
    started: float,
    version_ref: str | None = None,
    action: str | None = None,
) -> tuple[dict, dict | None]:
    """Fire the signed ``/deploy`` request and, when ``wait``, poll it to a terminal status.

    The one place deploy_v2 sends a runner request and awaits it, so the lost-request
    rule below holds on every path (#666). Returns ``(trigger response, final status)``;
    the final status is ``None`` when ``wait`` is false.

    A runner recreated between the request and the first poll has no record of the
    deploy (:class:`RunnerLostDeploymentError`). The runner de-duplicates a repeated
    ``/deploy``, and a sync of an unchanged configuration is a no-op (SSOT ops.pipeline
    section 5), so a ``staging`` deploy sends the request once more. A ``production``
    deploy raises the original error at once: a person decides. A second loss raises with
    both deployment ids. Every other error (a timeout, a failed deploy, a bad argument)
    passes through, as the runner may still hold that deploy.
    """
    runner_args = {
        key: value
        for key, value in (("version_ref", version_ref), ("action", action))
        if value is not None
    }
    submissions = _STAGING_SUBMISSIONS if env == "staging" else 1
    lost_ids: list[str] = []
    while True:
        response = trigger_platform_deploy(
            env=env,
            ref=ref,
            services=services,
            base_url=url,
            secret=secret,
            triggered_by=triggered_by,
            wait=False,
            **runner_args,
        )
        if not wait:
            return response, None
        try:
            final = poll_platform_deploy_status(
                env=env,
                ref=ref,
                services=services,
                deployment_id=response.get("deployment_id"),
                base_url=url,
                secret=secret,
                triggered_by=triggered_by,
                attempts=_poll_attempts_for_timeout(timeout),
                **runner_args,
                **_STATUS_POLL_SCHEDULE,
            )
        except RunnerLostDeploymentError as lost:
            lost_ids.append(str(response.get("deployment_id") or "unknown"))
            if len(lost_ids) >= submissions:
                if submissions == 1:
                    raise
                raise RunnerLostDeploymentError(
                    f"iac_runner lost the request {len(lost_ids)} times for "
                    f"{', '.join(services)} to {env} (deployments {', '.join(lost_ids)}); "
                    f"it was re-submitted once and is not sent again: {lost}"
                ) from lost
            _progress(
                f"iac-runner request for {', '.join(services)} to {env}",
                "lost; re-submitting once",
                started,
            )
            continue
        return response, final


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
    response, final = _trigger_and_poll(
        env=runner_env,
        ref=iac_sha,
        services=[service],
        url=url,
        secret=sec,
        triggered_by=triggered_by,
        wait=True,
        timeout=timeout,
        started=started,
        action="secrets-supply",
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
    iac_ref: str = "",
    *,
    version_ref: str = "",
    options: DeployOptions | None = None,
    deps: DeployDeps | None = None,
) -> dict:
    """Route multiple iac_pinned services through one deploy_v2/iac_runner call.

    The normal public API remains single-service. This CLI helper exists for
    post-merge reconcile fan-out so a manifest-wide input change produces one
    terminal-status wait per environment, not one wait per service.
    """
    opt = options or DeployOptions()
    dep = deps or DeployDeps()
    if not services:
        raise ValueError("at least one service is required")
    type_spec = deploy_type_spec(deploy_type)
    if type_spec.env not in ("staging", "prod"):
        raise ValueError(
            f"iac_pinned batch deploys to staging/prod only "
            f"(type {deploy_type!r} -> env {type_spec.env!r})"
        )
    iac_clean = str(iac_ref or "").strip()
    ver_clean = str(version_ref or "").strip()
    if iac_clean and ver_clean and iac_clean != ver_clean:
        raise ValueError(
            f"platform batch accepts one infra2 ref; got conflicting version_ref={ver_clean!r} and iac_ref={iac_clean!r}"
        )
    effective_iac_ref = iac_clean or ver_clean or _default_iac_ref(type_spec.env)
    # The same two guards as deploy_v2(): a batch must not resolve a branch to a sha (#1628).
    validate_iac_ref_form(deploy_type, classify_ref(effective_iac_ref))
    assert_iac_ref_on_main(effective_iac_ref, deploy_type)
    iac_sha = resolve_to_sha(effective_iac_ref, repo=_INFRA2_REPO)
    targets: list[DeployTarget] = []
    data_lanes: set[str] = set()
    for service in services:
        spec = service_spec(service)
        if not spec.iac_pinned or spec.artifact_source != "infra2":
            raise ValueError(
                f"{service!r} (iac_pinned={spec.iac_pinned}, artifact_source={spec.artifact_source!r}); "
                "batch deploy only supports infra2-sourced services"
            )
        target = make_deploy_target(
            service=service, env=type_spec.env, code_version=iac_sha, iac_ref=iac_sha
        )
        validate_deploy_target(target, spec)
        data_lanes.add(
            enforce_data_lane_red_lines(target, code_reviewed=opt.code_reviewed)
        )
        targets.append(target)

    env = "production" if type_spec.env == "prod" else "staging"
    url = dep.iac_runner_url or os.getenv("IAC_RUNNER_URL", "")
    sec = dep.iac_webhook_secret or os.getenv("IAC_WEBHOOK_SECRET", "")
    started = time.monotonic()
    response, final = _trigger_and_poll(
        env=env,
        ref=iac_sha,
        services=services,
        url=url,
        secret=sec,
        triggered_by=opt.triggered_by,
        wait=opt.wait,
        timeout=opt.timeout,
        started=started,
    )
    detail = {"env": env, "ref": iac_sha, "services": services, "iac_runner": response}
    if opt.wait:
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

    iac_sha = resolve_to_sha(iac_ref, repo=_INFRA2_REPO)
    clone_ref = iac_ref.strip()

    return resolved, alias_value, iac_sha, clone_ref


def _deploy_preview_v2(
    target: DeployTarget,
    spec: DeployTypeSpec,
    alias_value: str,
    service: str,
    resolved: ResolvedRef,
    domain: str,
    client: Any,
    clone_ref: str,
    data_lane: list[str],
    *,
    iac_ref: str = "main",
    opt: DeployOptions,
) -> DeployV2Result:
    preview_service_config(
        service
    )  # the registry is the capability; fails before side effects
    previous_template = os.environ.get("VAULT_SECRETS_TEMPLATE")
    injected_template = False
    if opt.verify_manifests and previous_template is None:
        rendered = verify_app_manifests(
            service,
            source_sha=resolved.sha,
            deploy_type=spec.key,
            iac_ref=iac_ref,
        )
        encoded = encode_rendered_templates(rendered, service=service)
        if encoded:
            os.environ["VAULT_SECRETS_TEMPLATE"] = encoded
            injected_template = True
    try:
        result = _preview_up(
            spec.alias_kind,
            alias_value,
            code=resolved.sha,
            options=PreviewOptions(
                service=service,
                image_ref=resolved.image_ref,
                iac_ref=target.iac_ref,
                branch=clone_ref,
                wait=opt.wait,
                health_timeout=opt.timeout,
            ),
            deps=PreviewDeps(
                client=client,
                domain=domain,
                repo=_repo_for_service(service),
            ),
        )
    finally:
        if injected_template:
            os.environ.pop("VAULT_SECRETS_TEMPLATE", None)
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
    data_lane: list[str],
    *,
    iac_ref: str = "main",
    opt: DeployOptions,
    dep: DeployDeps,
) -> DeployV2Result:
    previous_template = os.environ.get("VAULT_SECRETS_TEMPLATE")
    injected_template = False
    if opt.verify_manifests and previous_template is None:
        rendered = verify_app_manifests(
            service,
            source_sha=resolved.sha,
            deploy_type=target.env,
            iac_ref=iac_ref,
        )
        encoded = encode_rendered_templates(rendered, service=service)
        if encoded:
            os.environ["VAULT_SECRETS_TEMPLATE"] = encoded
            injected_template = True
    try:
        secret_supply = _supply_app_secrets(
            service,
            target.env,
            target.iac_ref,
            runner_url=dep.iac_runner_url,
            secret=dep.iac_webhook_secret,
            triggered_by=opt.triggered_by,
            timeout=opt.timeout,
        )
        promote_started = time.monotonic()
        plan = _deploy_fixed(
            target.env,
            resolved.sha,
            options=PromoteOptions(
                service=service,
                image_ref=resolved.image_ref,
                iac_ref=target.iac_ref,
                branch=clone_ref,
                wait=opt.wait,
                timeout=opt.timeout,
                staging_validated=opt.staging_validated,
                break_glass=opt.break_glass,
                force_promote=opt.force_promote,
                pause_rollback=opt.pause_rollback,
                max_rollback_attempts=opt.max_rollback_attempts,
                verify_vault=opt.verify_vault,
                verify_config=opt.verify_config,
                verify_smoke=opt.verify_smoke,
                verify_ingestion=opt.verify_ingestion,
            ),
            deps=PromoteDeps(
                client=client,
                domain=domain,
                repo=_repo_for_service(service),
            ),
        )
    finally:
        if injected_template:
            os.environ.pop("VAULT_SECRETS_TEMPLATE", None)
    _progress(
        f"Dokploy promote of {service} to {target.env}",
        "in service" if opt.wait else "triggered",
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
        "force_promote": opt.force_promote,
        "pause_rollback": opt.pause_rollback,
        "max_rollback_attempts": opt.max_rollback_attempts,
    }
    return DeployV2Result(target, data_lane, "deploy-primitive", detail)


def _resolve_control_plane_iac_ref(
    svc_spec: ServiceSpec,
    spec: DeployTypeSpec,
    version_ref: Any,
    iac_ref: str | None,
) -> tuple[str, Any]:
    """Derive ``(effective_iac_ref, effective_version_ref)`` inside the control plane.

    - For ``artifact_source == "infra2"`` in ``staging``/``prod``: ``version_ref`` IS the
      infra2 ref (accepting either ``version_ref`` or ``iac_ref``, rejecting conflicts, and
      defaulting to ``newest_release_tag`` when both are empty).
    - For ``artifact_source == "infra2"`` in ``preview``: ``version_ref`` selects the preview
      slot and ``iac_ref`` defaults to ``"main"`` when omitted.
    - For ``artifact_source == "app"``: ``iac_ref`` defaults to ``newest_release_tag`` on
      ``staging``/``prod`` and ``"main"`` on ``preview``.
    """
    iac_clean = str(iac_ref or "").strip()
    ver_clean = str(version_ref).strip() if version_ref is not None else ""
    if svc_spec.artifact_source == "infra2":
        if spec.env == "preview":
            return (iac_clean or _default_iac_ref(spec.env)), version_ref
        if ver_clean and iac_clean and ver_clean != iac_clean:
            raise ValueError(
                f"service {svc_spec.key!r} accepts one infra2 ref; got conflicting "
                f"version_ref={ver_clean!r} and iac_ref={iac_clean!r}"
            )
        effective_iac = iac_clean or ver_clean or _default_iac_ref(spec.env)
        if ver_clean:
            validate_ref_form(spec.key, classify_ref(ver_clean))
        return effective_iac, ""
    effective_iac = iac_clean or _default_iac_ref(spec.env)
    return effective_iac, version_ref


def deploy_v2(
    service: str = _APP_SERVICE,
    deploy_type: str = "staging",
    version_ref: Any = "",
    *,
    iac_ref: str = "",
    options: DeployOptions | None = None,
    deps: DeployDeps | None = None,
) -> DeployV2Result:
    """Execute one orthogonal deploy coordinate ``(service, deploy_type, version_ref)``."""
    opt = options or DeployOptions()
    dep = deps or DeployDeps()
    if opt.force_promote:
        print(
            f"[AUDIT] deploy_v2 force_promote=True for {service} ({deploy_type}, {version_ref!r})",
            file=sys.stderr,
        )
    if opt.pause_rollback:
        print(
            f"[AUDIT] deploy_v2 pause_rollback=True for {service} ({deploy_type}, {version_ref!r})",
            file=sys.stderr,
        )
    if opt.bootstrap:
        print(
            f"[AUDIT] deploy_v2 bootstrap=True for {service} ({deploy_type}, {version_ref!r})",
            file=sys.stderr,
        )
    effective_domain = (
        domain_for_service(service)
        or (str(dep.domain).strip() if dep.domain is not None else "")
        or infra_domain()
    )
    svc_spec = service_spec(service)
    normalized_expected_sha = _normalize_expected_sha(opt.expected_sha)
    spec = resolve_deploy_type_for_ref(deploy_type, version_ref)
    effective_iac_ref, effective_input_version = _resolve_control_plane_iac_ref(
        svc_spec, spec, version_ref, iac_ref
    )
    validate_iac_ref_form(spec.key, classify_ref(effective_iac_ref))
    assert_iac_ref_on_main(effective_iac_ref, spec.key)
    effective_client = dep.client
    if effective_client is None and (not svc_spec.iac_pinned or spec.env == "preview"):
        from common.deploy.dokploy_client import get_dokploy

        effective_client = get_dokploy(host=f"cloud.{infra_domain()}")

    if svc_spec.iac_pinned:  # platform or companion service -> iac_runner / preview
        if normalized_expected_sha is not None:
            raise ValueError("--expected-sha is only supported for app-backed deploys")
        if spec.env == "preview":
            return _deploy_platform_preview(
                service,
                spec.key,
                effective_input_version,
                effective_iac_ref,
                client=effective_client,
                domain=effective_domain,
                opt=opt,
            )
        return _deploy_platform(
            service,
            spec.key,
            effective_iac_ref,
            version_ref=effective_input_version,
            options=opt,
            deps=dep,
        )

    effective_version_ref = effective_input_version
    if spec.env in ("staging", "prod") and not (
        str(effective_version_ref).strip() if effective_version_ref is not None else ""
    ):
        try:
            train_tag = default_version_ref(service, iac_ref=effective_iac_ref)
        except TypeError:
            train_tag = default_version_ref(service)
        if train_tag:
            effective_version_ref = train_tag

    resolved, alias_value, iac_sha, clone_ref = _resolve_app_refs(
        service,
        spec,
        effective_version_ref,
        effective_iac_ref,
        normalized_expected_sha,
        dep.repo,
    )
    if spec.env in ("staging", "prod"):
        assert_app_version_ref_matches_train(
            service,
            str(effective_version_ref).strip(),
            spec.key,
            bootstrap=opt.bootstrap,
            iac_ref=effective_iac_ref,
        )
        assert_app_version_ref_on_main(
            str(effective_version_ref).strip(),
            spec.key,
            service=service,
            repo=dep.repo,
        )
    target = make_target(
        spec.key,
        service=service,
        version=resolved.sha,
        iac_ref=iac_sha,
        alias_value=alias_value,
    )
    validate_deploy_target(target, service_spec(service))
    data_lane = enforce_data_lane_red_lines(target, code_reviewed=opt.code_reviewed)

    if env_config(spec.env).requires_staging_first and not (
        opt.staging_validated or opt.break_glass or opt.force_promote
    ):
        raise ValueError(
            f"deploy type {spec.key!r} requires a prior staging deploy "
            "(pass staging_validated, or break_glass for an emergency)"
        )

    _wait_for_image_dependencies(
        svc_spec,
        resolved.image_ref,
        timeout=opt.image_wait_seconds,
        poll_seconds=opt.image_poll_seconds,
    )

    if env_config(target.env).dynamic:
        return _deploy_preview_v2(
            target,
            spec,
            alias_value,
            service,
            resolved,
            effective_domain,
            effective_client,
            clone_ref,
            data_lane,
            iac_ref=effective_iac_ref,
            opt=opt,
        )

    return _deploy_fixed_v2(
        target,
        resolved,
        service,
        effective_domain,
        effective_client,
        clone_ref,
        data_lane,
        iac_ref=effective_iac_ref,
        opt=opt,
        dep=dep,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="unified deploy_v2 front door")
    parser.add_argument("--service", default=_APP_SERVICE, help="service key")
    parser.add_argument(
        "--type",
        required=True,
        dest="deploy_type",
        help="deploy type: staging | prod | preview (also accepts preview/branch, preview/pr)",
    )
    parser.add_argument(
        "--version-ref",
        default="",
        help="version surface: pr-<N> or 1-6 digit PR# (preview), main (preview), or release "
        "tag vX.Y.Z (staging / prod; defaults to harness/repos.yaml for apps or latest infra2 "
        "release tag for platform services)",
    )
    parser.add_argument(
        "--iac-ref",
        default="",
        help="optional infra2 ref override (control plane defaults to latest release tag on "
        "staging/prod and main on preview)",
    )
    parser.add_argument(
        "--domain",
        default="",
        help="optional base domain override (defaults to service domain or infra_domain())",
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
        "--bootstrap",
        action="store_true",
        help="allow a staging/prod app deploy with a version_ref not matching harness/repos.yaml",
    )
    parser.add_argument(
        "--force-promote",
        action="store_true",
        help="bypass false-positive schema/smoke gates and promote with audit trail (#722)",
    )
    parser.add_argument(
        "--pause-rollback",
        action="store_true",
        help="suppress automatic rollback on post-deploy gate failure with audit trail (#722)",
    )
    parser.add_argument(
        "--max-rollback-attempts",
        type=int,
        default=2,
        help="maximum automatic rollback attempts before circuit breaker trips (default: 2)",
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
        "--no-verify-smoke",
        action="store_true",
        help="skip the post-swap public /api/health version-identity check (default: verify)",
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
        help="seconds between image artifact readiness checks (default: 10)",
    )
    parser.add_argument(
        "--no-verify-manifests",
        action="store_true",
        help="skip fetching upstream App manifests and rendering VAULT_SECRETS_TEMPLATE",
    )
    return parser


def _handle_teardown(args: argparse.Namespace) -> int:
    spec = resolve_deploy_type_for_ref(args.deploy_type, args.version_ref)
    if spec.env != "preview":
        raise ValueError(
            f"--down only tears down preview/* aliases; type {args.deploy_type!r} "
            f"-> env {spec.env!r} has no ephemeral alias to remove"
        )
    if spec.alias_kind == "branch":
        alias_value = _default_main(args.version_ref)
    else:
        alias_value = str(normalize_pr_number(args.version_ref))
    from common.deploy.dokploy_client import get_dokploy

    domain = _validate_domain(
        domain_for_service(args.service) or args.domain or infra_domain()
    )
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
                "containers_cleared": down_result.containers_cleared,
            }
        )
    )
    return 0 if down_result.containers_cleared else 1


def _handle_platform_batch(args: argparse.Namespace, service_names: list[str]) -> int:
    result = _deploy_platform_batch(
        service_names,
        args.deploy_type,
        args.iac_ref,
        version_ref=args.version_ref,
        options=DeployOptions(
            code_reviewed=True if args.code_reviewed else None,
            wait=not args.no_wait,
            timeout=args.timeout,
        ),
        deps=DeployDeps(
            iac_runner_url=os.getenv("IAC_RUNNER_URL", ""),
            iac_webhook_secret=os.getenv("IAC_WEBHOOK_SECRET", ""),
        ),
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
        if (
            not service_spec(args.service).iac_pinned
            or deploy_type_spec(args.deploy_type).env == "preview"
        ):
            from common.deploy.dokploy_client import get_dokploy

            client = get_dokploy(host=f"cloud.{infra_domain()}")

        options = DeployOptions(
            wait=not args.no_wait,
            timeout=args.timeout,
            staging_validated=args.staging_validated,
            code_reviewed=True if args.code_reviewed else None,
            break_glass=args.break_glass,
            bootstrap=args.bootstrap,
            force_promote=args.force_promote,
            pause_rollback=args.pause_rollback,
            max_rollback_attempts=args.max_rollback_attempts,
            verify_vault=not args.skip_vault_check,
            verify_config=not args.no_verify_config,
            verify_smoke=not args.no_verify_smoke,
            verify_ingestion=args.verify_ingestion,
            verify_manifests=not args.no_verify_manifests,
            image_wait_seconds=args.image_wait_seconds,
            image_poll_seconds=args.image_poll_seconds,
            expected_sha=args.expected_sha,
        )
        deps = DeployDeps(
            client=client,
            domain=args.domain or None,
            repo=args.repo,
        )
        result = deploy_v2(
            service=args.service,
            deploy_type=args.deploy_type,
            version_ref=args.version_ref,
            iac_ref=args.iac_ref,
            options=options,
            deps=deps,
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
