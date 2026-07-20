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
import math
import os
import sys
import time
from dataclasses import dataclass

import httpx  # Dokploy transport errors from libs.dokploy surface as httpx exceptions

from libs.iac_runner_client import (
    poll_platform_deploy_status,
    trigger_platform_deploy,
)
from libs.deploy_contract import (
    _SHA_RE,
    SERVICES,
    DeployTarget,
    deploy_type_spec,
    is_tag_only_iac_env,
    make_deploy_target,
    make_target,
    service_spec,
    validate_deploy_target,
    validate_iac_ref_form,
    validate_ref_form,
)
from libs.deploy_env_config import env_config
from libs.deploy.promote import deploy as _deploy_fixed
from libs.deploy.promote import model_overrides_from_env
from libs.deploy.preview import _validate_domain
from libs.deploy.preview import down as _preview_down
from libs.deploy.preview import up as _preview_up
from tools.resolve_deploy_ref import (
    classify_ref,
    resolve_image_ref,
    resolve_pr,
    resolve_to_sha,
)

_APP_SERVICE = "finance_report/app"
_APP_REPO = "https://github.com/wangzitian0/finance_report.git"
# Every app-backed service's version_ref (tag/sha) resolves against its OWN source repo.
# Mirrors libs.app_deploy_request.APP_SOURCES (kept in sync there for the cross-repo
# request contract's short "owner/repo" form) — a service missing here falls back to
# _APP_REPO, which was the sole hardcoded target before this map existed (finance_report
# was the only service, so it went unnoticed until truealpha's first automated deploy
# resolved its `v0.0.3` tag against finance_report's own unrelated old `v0.0.3` tag).
_SERVICE_REPOS: dict[str, str] = {
    _APP_SERVICE: _APP_REPO,
    "truealpha/app": "https://github.com/wangzitian0/truealpha.git",
}
_INFRA2_REPO = "https://github.com/wangzitian0/infra2"
# Dokploy's github source clones a branch/tag ref (`git clone -b`), NOT a commit sha — a
# raw sha fails "Remote branch <sha> not found" (finance_report#342). So when iac_ref is a
# sha we clone the default branch; a branch/tag iac_ref is cloned verbatim (this is what
# dissolves the old separate `iac_branch` input — the iac_ref surface now drives the clone).
_INFRA2_DEFAULT_BRANCH = "main"
# The canary runs arbitrary code on a fixed throwaway preview slot no real PR reuses.
_CANARY_PR = 999
_DEFAULT_IMAGE_WAIT_SECONDS = 300
_DEFAULT_IMAGE_POLL_SECONDS = 10.0
_IMAGE_MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


def _infra2_owner_name(repo: str) -> str:
    """`https://github.com/wangzitian0/infra2[.git]` -> `wangzitian0/infra2`."""
    return repo.rstrip("/").removesuffix(".git").split("github.com/")[-1]


def assert_iac_ref_on_main(
    iac_ref: str,
    deploy_type: str,
    *,
    repo: str = _INFRA2_REPO,
    token: str | None = None,
    transport=httpx.get,
) -> None:
    """Fail-closed: a fixed-env (staging/prod) ``iac_ref`` must be an ON-MAIN release tag —
    its commit reachable from infra2 ``main``.

    The app-side twin of reconcile's ``assert_after_on_main``, closing the third app↔infra
    boundary line (#465): the app may only run a reviewed, *released* infra version, never an
    off-main/feature-branch tag. Uses GitHub's compare API (``main...<ref>``): status
    ``behind``/``identical`` means reachable from main; ``ahead``/``diverged`` is off-main and
    refused. preview/canary are exempt (they clone live refs). Any transport/API failure is
    fail-closed (it raises rather than letting an unverified ref through). ``transport`` and
    ``token`` are injected for tests.
    """
    if not is_tag_only_iac_env(deploy_type):
        return
    url = (
        f"https://api.github.com/repos/{_infra2_owner_name(repo)}"
        f"/compare/main...{iac_ref.strip()}"
    )
    headers = {"Accept": "application/vnd.github+json"}
    tok = token if token is not None else os.getenv("GITHUB_TOKEN", "").strip()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    resp = transport(url, headers=headers, timeout=30)
    resp.raise_for_status()
    status = (resp.json() or {}).get("status")
    if status not in ("behind", "identical"):
        raise ValueError(
            f"iac_ref {iac_ref!r} is not on infra2 main (compare status={status!r}); "
            f"staging/prod require an on-main release tag (#465 — the app-side twin of the "
            f"reconcile off-main guard). Re-cut/use a tag on main."
        )


def _default_main(version_ref) -> str:
    """A ``branch`` / ``canary`` version_ref defaults to the main tip when omitted."""
    return (str(version_ref).strip() if version_ref is not None else "") or "main"


def _repo_for_service(service: str) -> str:
    """The git repo a service's ``version_ref`` (tag/sha/branch) resolves against."""
    return _SERVICE_REPOS.get(service, _APP_REPO)


def _dokploy_host_domain(cli_domain: str) -> str:
    """The Dokploy control-plane host's domain — always the org's one shared zone.

    NEVER derived from a service's own public ``--domain`` (``Deployer.domain``, e.g.
    truealpha/app -> truealpha.club, #550): there is exactly one Dokploy instance for
    every service, always reachable at ``cloud.<org domain>``. ``INTERNAL_DOMAIN`` (set
    org-wide by both deploy workflows, never per-service-overridden) is authoritative
    when present; ``--domain`` is only a same-value fallback for local/manual runs that
    don't export it. Using ``--domain`` here directly was the #550 regression: truealpha's
    first staging deploy tried to reach the nonexistent ``cloud.truealpha.club``.
    """
    return os.getenv("INTERNAL_DOMAIN") or cli_domain


def _resolve_for_type(spec, version_ref, *, repo: str):
    """Resolve a type's ``version_ref`` surface to ``(ResolvedRef, alias_value)``.

    The ``type`` decides how ``version_ref`` is read (a discriminated union), and the
    matrix (``accepted_forms``) fails closed on a form the type does not take:

    - ``canary``          — any ref form, code OR release (default main); runs on the
                            fixed ``pr-<_CANARY_PR>`` slot (it is a deploy-path probe, so
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
        return resolve_image_ref(ref, repo=repo), _CANARY_PR
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


def _env_number(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {raw!r}")
    if value < 0:
        raise ValueError(f"{name} must be non-negative, got {value}")
    return value


def _registry_image_parts(image: str) -> tuple[str, str]:
    registry, sep, repository = image.partition("/")
    if not sep or not registry or not repository:
        raise ValueError(
            f"image repository must include registry and repository path, got {image!r}"
        )
    return registry, repository


def _parse_bearer_authenticate(header: str) -> dict[str, str]:
    scheme, _, rest = header.partition(" ")
    if scheme.lower() != "bearer":
        raise RuntimeError("registry did not return a Bearer authentication challenge")
    params: dict[str, str] = {}
    for item in rest.split(","):
        key, sep, value = item.strip().partition("=")
        if sep:
            params[key] = value.strip().strip('"')
    return params


def _registry_bearer_token(client: httpx.Client, authenticate_header: str) -> str:
    params = _parse_bearer_authenticate(authenticate_header)
    realm = params.get("realm")
    if not realm:
        raise RuntimeError("registry Bearer challenge did not include a token realm")
    token_params = {
        key: value for key in ("service", "scope") if (value := params.get(key))
    }
    response = client.get(realm, params=token_params)
    response.raise_for_status()
    payload = response.json()
    token = payload.get("token") or payload.get("access_token")
    if not token:
        raise RuntimeError("registry token response did not include a token")
    return str(token)


def _image_manifest_exists(
    image: str, image_ref: str, *, client: httpx.Client | None = None
) -> bool:
    """Return whether ``image:image_ref`` exists in the registry.

    The app images live in GHCR, but this uses the standard Docker Registry v2
    manifest API. Public GHCR packages often answer the first request with a
    Bearer challenge; in that case we fetch an anonymous pull token and retry.
    """
    registry, repository = _registry_image_parts(image)
    url = f"https://{registry}/v2/{repository}/manifests/{image_ref}"
    headers = {"Accept": _IMAGE_MANIFEST_ACCEPT}

    def request(c: httpx.Client, *, token: str | None = None) -> httpx.Response:
        req_headers = dict(headers)
        if token:
            req_headers["Authorization"] = f"Bearer {token}"
        return c.get(url, headers=req_headers)

    if client is None:
        with httpx.Client(timeout=10.0, follow_redirects=True) as created:
            return _image_manifest_exists(image, image_ref, client=created)

    response = request(client)
    if response.status_code == 401:
        token = _registry_bearer_token(
            client, response.headers.get("www-authenticate", "")
        )
        response = request(client, token=token)
    if 200 <= response.status_code < 300:
        return True
    if response.status_code == 404:
        return False
    if response.status_code in (401, 403):
        raise RuntimeError(
            f"registry refused manifest check for {image}:{image_ref} "
            f"(status {response.status_code})"
        )
    if response.status_code == 429 or response.status_code >= 500:
        raise RuntimeError(
            f"registry manifest check for {image}:{image_ref} is temporarily unavailable "
            f"(status {response.status_code})"
        )
    raise RuntimeError(
        f"registry manifest check for {image}:{image_ref} returned status "
        f"{response.status_code}"
    )


def _wait_for_image_dependencies(
    spec,
    image_ref: str,
    *,
    timeout: float | None = None,
    poll_seconds: float | None = None,
) -> None:
    """Wait until the service's declared image artifacts expose ``image_ref``.

    This closes the race where ``main`` resolves to a commit before the app repo has
    published all images for that commit. The check is before any Dokploy mutation, so a
    missing artifact fails as an input/readiness problem rather than creating a broken
    stack and paging on composeStatus=error.
    """
    repositories = tuple(getattr(spec, "image_repositories", ()) or ())
    if not repositories:
        return
    max_wait = _env_number("DEPLOY_V2_IMAGE_WAIT_SECONDS", _DEFAULT_IMAGE_WAIT_SECONDS)
    interval = _env_number("DEPLOY_V2_IMAGE_POLL_SECONDS", _DEFAULT_IMAGE_POLL_SECONDS)
    if timeout is not None:
        max_wait = float(timeout)
    if poll_seconds is not None:
        interval = float(poll_seconds)
    if not math.isfinite(max_wait) or not math.isfinite(interval):
        raise ValueError("image wait timeout and poll interval must be finite")
    if max_wait < 0 or interval < 0:
        raise ValueError("image wait timeout and poll interval must be non-negative")
    if max_wait > 0 and interval == 0:
        raise ValueError(
            "image poll interval must be positive when image wait is enabled"
        )

    deadline = time.monotonic() + max_wait
    last_missing: list[str] = []
    last_errors: list[str] = []
    while True:
        missing: list[str] = []
        errors: list[str] = []
        for image in repositories:
            try:
                if not _image_manifest_exists(image, image_ref):
                    missing.append(image)
            except (RuntimeError, httpx.HTTPError) as exc:
                errors.append(f"{image}: {exc}")
        if not missing and not errors:
            return
        last_missing = missing
        last_errors = errors
        if time.monotonic() >= deadline:
            parts = []
            if last_missing:
                parts.append(
                    "missing " + ", ".join(f"{i}:{image_ref}" for i in last_missing)
                )
            if last_errors:
                parts.append("errors " + "; ".join(last_errors))
            detail = "; ".join(parts) or "unknown registry readiness state"
            raise RuntimeError(
                f"required image artifacts for {spec.key} image_ref {image_ref!r} "
                f"not published after {max_wait:g}s: {detail}"
            )
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))


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
      caller cannot reach prod data by simply omitting the argument. Snapshot sync,
      anonymization, and rehearsal remain finance_report#893 scope, but ``data_lane`` is
      already derived here and is not a public deploy input.
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
) -> DeployV2Result:
    """Route a platform (iac_pinned) service to the iac_runner ``/deploy`` webhook.

    We do NOT re-implement the platform deploy — ``Deployer.sync`` is Context/os.environ
    coupled — we trigger the SAME signed webhook ``deploy.yml`` uses, so the deploy
    is byte-for-byte iac_runner's. ``version_ref`` is unused: a platform artifact IS the
    ``iac_ref``-pinned stack, so the deploy ref (and the recorded version identity) is the
    resolved infra2 sha. Platform services have no preview — only ``staging`` / ``prod``.
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
    response = trigger_platform_deploy(
        env=env,
        ref=iac_sha,
        services=[service],
        base_url=url,
        secret=sec,
        triggered_by=triggered_by,
        wait=False,
    )
    detail = {"env": env, "ref": iac_sha, "services": [service], "iac_runner": response}
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
        )
        detail["iac_runner_final"] = final
        status = str(final.get("status", "")).lower()
        if status != "completed":  # "failed" / anything non-success
            raise RuntimeError(
                f"platform deploy of {service} to {env} ended {status!r}: "
                f"{final.get('details') or final}"
            )
    return DeployV2Result(target, data_lane, "iac-runner", detail)


def _poll_attempts_for_timeout(timeout: int, interval: int = 10) -> int:
    """Convert a seconds budget into iac_runner status poll attempts."""
    return max(1, (max(1, int(timeout)) + interval - 1) // interval)


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
        )
        detail["iac_runner_final"] = final
        status = str(final.get("status", "")).lower()
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
    """Execute one deploy_v2 coordinate ``(service, type, version_ref, iac_ref)``.

    The ``type`` is the discriminant: it interprets ``version_ref`` (a PR# / sha / tag /
    branch — :func:`_resolve_for_type`), fails closed on a form it does not accept, and
    declares its gates. ``version_ref`` resolves to the commit identity (``sha``) AND the
    published ``image_ref`` the backend pulls (a short sha for code, a retained tag for a
    release). ``iac_ref`` (a branch/tag/sha of infra2) pins the IaC and, when cloneable,
    the preview compose template.

    Raises ``ValueError`` for any contract / form / gate / red-line / unsupported-service
    violation BEFORE any side effect; backend errors (rollout / health) propagate.

    A platform (``iac_pinned``) service routes to the iac_runner webhook instead, ignoring
    ``version_ref`` (its artifact is the ``iac_ref``-pinned stack) — see :func:`_deploy_platform`.
    """
    svc_spec = service_spec(service)
    normalized_expected_sha = _normalize_expected_sha(expected_sha)
    # Fixed envs (staging/prod) pin their IaC to an immutable release tag, on BOTH the
    # app-image axis (version_ref, gated per-type below) and the infra2-IaC axis (iac_ref,
    # gated here BEFORE the platform/app branch so it covers both). A sha/branch iac_ref can
    # no longer reach a fixed env — the gap that let a main-sha reconcile auto-deploy to prod.
    # preview/canary keep an unrestricted iac_ref (they clone live refs).
    validate_iac_ref_form(deploy_type, classify_ref(iac_ref))
    # #465: a fixed-env iac_ref must also be ON infra2 main (not just tag-shaped) — the app
    # only runs reviewed, released infra. Covers both the platform and app branches below.
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
        )

    spec = deploy_type_spec(deploy_type)
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

    # Gate: prod promotes code already validated on staging. The policy is owned by the
    # env (single source: env_config(...).requires_staging_first), not re-declared on the
    # type; break_glass is the audited emergency bypass.
    if env_config(spec.env).requires_staging_first and not (
        staging_validated or break_glass
    ):
        raise ValueError(
            f"deploy type {deploy_type!r} requires a prior staging deploy "
            "(pass staging_validated, or break_glass for an emergency)"
        )

    if service not in SERVICES:
        raise ValueError(
            f"{service!r} is not an app-backed service and is not marked iac_pinned; "
            "deploy_v2 only routes services explicitly registered as app-backed "
            "(libs.deploy_contract.SERVICES) or iac_pinned services derived from "
            "libs.service_registry."
        )

    _wait_for_image_dependencies(
        svc_spec,
        resolved.image_ref,
        timeout=image_wait_seconds,
        poll_seconds=image_poll_seconds,
    )

    if env_config(target.env).dynamic:  # preview (incl. canary)
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

    # staging | prod: the fixed-compose promote path. The backend pulls image_ref (a tag
    # for a release, the short sha for code); iac_ref is carried on the target for the record.
    # verify_vault / verify_config / model_overrides give the unified path PARITY with the
    # old bash dokploy_deploy.sh — default-ON so a token-TTL/effective-config regression
    # fails closed instead of being silently dropped on the way to prod.
    plan = _deploy_fixed(
        target.env,
        resolved.sha,
        domain=domain,
        client=client,
        service=service,
        image_ref=resolved.image_ref,
        iac_ref=target.iac_ref,
        wait=wait,
        timeout=timeout,
        staging_validated=staging_validated,
        break_glass=break_glass,
        verify_vault=verify_vault,
        verify_config=verify_config,
        verify_ingestion=verify_ingestion,
        model_overrides=model_overrides_from_env(),
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
    """CLI entry for the unified front door — the surface deploy workflows invoke.

    Builds the Dokploy client, runs ``deploy_v2`` (which resolves the version_ref/iac_ref
    surfaces itself), and prints the result as one JSON line.
    """
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
    args = parser.parse_args(argv)

    try:
        # Teardown: the operator command the retired preview-lifecycle CLI exposed as
        # its `down` subcommand, now folded into this front door.
        # deploy_v2's coordinate covers preview UP and the staging/prod promote, but not
        # tearing a preview alias back down — this flag closes that gap by resolving the
        # SAME alias (alias_kind from --type, value from --version-ref, exactly as `up`
        # reads them) and calling the preview backend's idempotent down(). Preview is always
        # a Dokploy stack (never iac_pinned), so it builds the Dokploy client directly.
        if args.down:
            spec = deploy_type_spec(args.deploy_type)
            if spec.env != "preview":
                raise ValueError(
                    f"--down only tears down preview/* aliases; type {args.deploy_type!r} "
                    f"-> env {spec.env!r} has no ephemeral alias to remove"
                )
            # Mirror `up`'s value read: branch defaults to the main tip; pr/commit/tag take
            # --version-ref verbatim (preview_alias normalizes a commit to its short sha).
            alias_value = (
                _default_main(args.version_ref)
                if spec.alias_kind == "branch"
                else args.version_ref
            )
            from libs.dokploy import get_dokploy

            # Reject a malformed domain before it reaches the Dokploy host string — the
            # same guard the preview backend applies on `up` (whitespace/empty would
            # corrupt cloud.<domain>).
            domain = _validate_domain(args.domain)
            down_result = _preview_down(
                spec.alias_kind,
                alias_value,
                domain=domain,
                client=get_dokploy(host=f"cloud.{_dokploy_host_domain(args.domain)}"),
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

        service_names = [s.strip() for s in args.service.split(",") if s.strip()]
        if len(service_names) > 1:
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

        # Platform (iac_pinned) services route to the iac_runner webhook and never touch the
        # Dokploy client — don't build it (or require DOKPLOY_API_KEY) for them. Inside the
        # try so an unknown service surfaces as the clean one-line error, not a traceback.
        client = None
        if not service_spec(args.service).iac_pinned:
            # Imported lazily so importing the module needs no Dokploy creds.
            from libs.dokploy import get_dokploy

            client = get_dokploy(host=f"cloud.{_dokploy_host_domain(args.domain)}")
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
