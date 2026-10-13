"""Fail-closed planning for cross-repository application deploy requests."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from infra2_sdk.deploy import (
    DeployOperation,
    DeployRequest,
    DeployType,
    verify_production_evidence,
)
from infra2_sdk.refs import ResolvedRef, resolve_image_ref, resolve_pr

from libs.core.constants import APP_SOURCES
from libs.core.registry import domain_for_service
from libs.deploy.release_markers import (
    MINIMUM_PRODUCTION_MARKER,
    PRODUCTION_MARKER_PREFIX,
    marker_status,
    newest_release_tag,
    production_marker,
    version_key,
)

# Re-exported so the receiver keeps one import surface (`marker_status` is used by
# tools/app_deploy_request.py's `markers` action, which must not import this module).
__all__ = [
    "ALLOWED_SENDERS",
    "APP_SOURCES",
    "DeployPlan",
    "FIXED_DEPLOY_TYPES",
    "MINIMUM_PRODUCTION_MARKER",
    "PREFLIGHT_CANARY_DEPLOY_TYPES",
    "PRODUCTION_REQUEST_MAX_AGE_SECONDS",
    "check_request_age",
    "make_plan",
    "marker_status",
    "newest_release_tag",
    "parse_request",
    "production_marker",
    "select_iac_ref",
    "validate_request_authority",
    "verify_production_evidence",
]

ALLOWED_SENDERS = frozenset({"wangzitian0"})
FIXED_DEPLOY_TYPES = frozenset({DeployType.STAGING, DeployType.PRODUCTION})
#: The deploy types whose request is canaried before it executes (truealpha#860):
#: production only. app-deploy-request.yml's preflight_canary pre-filter mirrors this set;
#: libs/tests/test_app_deploy_request_workflow.py pins the two to each other and the
#: receiver re-checks it at run time, so they cannot drift apart silently.
PREFLIGHT_CANARY_DEPLOY_TYPES = frozenset({DeployType.PRODUCTION})
#: The maximum age of a production request when the receiver checks it: 24 min (#1232).
#: Two senders wait for a receiver run. The receiver cannot read their deadlines, so
#: one limit serves both, and it is set for the sender that stops first:
#: - finance_report release.yml: a 30 min job limit stops the sender 1739 s to 1761 s
#:   after the receiver run is created (13 runs), so about 1740 s.
#: - truealpha deploy-release.yml: 360 polls at 5 s intervals. Its sender stopped 2178 s
#:   after the receiver run was created (run 38344114882).
#: The deploy job then needs up to 293 s (the longest of 25 production runs), so the
#: limit keeps 300 s for it: about 1740 s minus 300 s is 24 min. For truealpha, the
#: limit is conservative. A request that starts near the limit can still end after the
#: finance_report sender stops. docs/ssot/ops.pipeline.md section 4.1 item 7 has the
#: details and the gap that remains.
PRODUCTION_REQUEST_MAX_AGE_SECONDS = 24 * 60


@dataclass(frozen=True)
class DeployPlan:
    request: DeployRequest
    iac_ref: str
    domain: str
    timeout: int
    # What the newest infra2 release is, next to the one this deploy will actually run
    # at. For production those are two different coordinates and their gap is the
    # promotion lag that made #650 invisible until a prod recreate had happened; naming
    # both in the plan means the lag is on screen before anything is deployed.
    newest_iac_tag: str = ""

    def deploy_v2_args(self) -> list[str]:
        args = [
            "--service",
            self.request.service,
            "--type",
            self.request.deploy_type.value,
            "--version-ref",
            self.request.version_ref,
            "--iac-ref",
            self.iac_ref,
            "--domain",
            self.domain,
            "--timeout",
            str(self.timeout),
        ]
        if self.request.operation == DeployOperation.REMOVE:
            args.append("--down")
        else:
            args.extend(["--expected-sha", self.request.source_sha])
        if self.request.deploy_type == DeployType.PRODUCTION:
            # Validation requires explicit staging and review evidence before these
            # existing deploy_v2 red-line acknowledgements can be asserted.
            args.extend(["--staging-validated", "--code-reviewed"])
        return args

    @property
    def requires_preflight_canary(self) -> bool:
        """Whether the receiver must canary this exact coordinate before executing it.

        The single source of truth for "which requests get gated". app-deploy-request.yml
        can only skip the canary job before any plan exists, so it pre-filters on the
        payload; the receiver refuses to run when that pre-filter disagrees with this
        property (``tools.app_deploy_request --require-preflight-canary`` /
        ``--preflight-canary-result``) and a workflow test pins the two together.

        Production only (truealpha#860). A production release runs at the production
        marker (:func:`select_iac_ref`), a coordinate nothing deployed before it, so the
        canary on the reserved slot is the only proof ahead of the mutation that
        ``version_ref`` runs at that ``iac_ref``; it keeps gating the promote. A staging
        deploy proves its own coordinate: deploy_v2 pins ``version_ref`` to
        ``source_sha``, accepts only an on-main release tag, waits for the images, and
        promotes to a new rollout record, the pushed config hash and every healthchecked
        container in service; the sending app then confirms the public release identity
        on staging, and production evidence requires that staging run to have succeeded.
        The canary that used to precede it proved that same coordinate on a throwaway
        slot and cost 81 s of every staging release (receiver run 35052266070).
        """
        return (
            self.request.deploy_type in PREFLIGHT_CANARY_DEPLOY_TYPES
            and self.request.operation != DeployOperation.REMOVE
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "request": self.request.to_dict(),
            "iac_ref": self.iac_ref,
            "domain": self.domain,
            "timeout": self.timeout,
            "deploy_v2_args": self.deploy_v2_args(),
            "requires_preflight_canary": self.requires_preflight_canary,
            "newest_iac_tag": self.newest_iac_tag,
        }


def parse_request(payload: str | Mapping[str, object]) -> DeployRequest:
    if isinstance(payload, str):
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"deploy request is not valid JSON: {exc.msg}") from None
    else:
        raw = payload
    if not isinstance(raw, Mapping):
        raise ValueError("deploy request must be a JSON object")
    return DeployRequest.from_dict(raw)


def validate_request_authority(
    request: DeployRequest,
    *,
    sender: str,
    production_evidence_verifier: Callable[[DeployRequest], None] | None = None,
    resolve_image: Callable[..., ResolvedRef] = resolve_image_ref,
    resolve_pull: Callable[..., ResolvedRef] = resolve_pr,
) -> None:
    if sender not in ALLOWED_SENDERS:
        raise ValueError(f"sender {sender!r} is not allowed to request deployments")
    expected_source = APP_SOURCES.get(request.service)
    if expected_source is None:
        raise ValueError(
            f"service {request.service!r} is not enabled for app deploy requests"
        )
    if request.source_repository != expected_source:
        raise ValueError(
            f"service {request.service!r} requires source_repository {expected_source!r}"
        )

    _validate_evidence_urls(request)
    if request.deploy_type == DeployType.PRODUCTION:
        verifier = production_evidence_verifier or verify_production_evidence
        verifier(request)
    if request.operation == DeployOperation.REMOVE:
        return

    if request.deploy_type == DeployType.PREVIEW_PR:
        resolved = resolve_pull(request.version_ref, repo=_git_url(expected_source))
    else:
        resolved = resolve_image(request.version_ref, repo=_git_url(expected_source))
    if resolved.sha.lower() != request.source_sha:
        raise ValueError(
            f"version_ref {request.version_ref!r} resolves to {resolved.sha.lower()}, "
            f"not source_sha {request.source_sha}"
        )


def select_iac_ref(
    deploy_type: DeployType,
    *,
    repo_root: str | Path,
    runner=subprocess.run,
) -> str:
    if deploy_type not in FIXED_DEPLOY_TYPES:
        return "main"
    if deploy_type != DeployType.PRODUCTION:
        return newest_release_tag(repo_root=repo_root, runner=runner)
    marker = production_marker(repo_root=repo_root, runner=runner)
    if version_key(marker) < version_key(MINIMUM_PRODUCTION_MARKER):
        raise ValueError(
            f"production marker {PRODUCTION_MARKER_PREFIX}{marker} predates "
            f"{MINIMUM_PRODUCTION_MARKER}, the first release whose deployer pins the "
            "data engine from the release being promoted (#632): promote infra2 to "
            "production first, or this deploy recreates the data engine from the "
            "digest an operator last wrote to Vault (#650)"
        )
    return marker


def make_plan(
    payload: str | Mapping[str, object],
    *,
    sender: str,
    domain: str,
    timeout: int,
    repo_root: str | Path,
    production_evidence_verifier: Callable[[DeployRequest], None] | None = None,
    resolve_image: Callable[..., ResolvedRef] = resolve_image_ref,
    resolve_pull: Callable[..., ResolvedRef] = resolve_pr,
    runner=subprocess.run,
) -> DeployPlan:
    request = parse_request(payload)
    validate_request_authority(
        request,
        sender=sender,
        production_evidence_verifier=production_evidence_verifier,
        resolve_image=resolve_image,
        resolve_pull=resolve_pull,
    )
    # A service with its own dedicated domain (Deployer.domain, e.g. truealpha/app ->
    # truealpha.club) overrides whatever shared INTERNAL_DOMAIN the caller passed in;
    # every other service (no override declared) keeps today's behavior unchanged.
    effective_domain = domain_for_service(request.service) or domain
    if not effective_domain or any(
        character.isspace() for character in effective_domain
    ):
        raise ValueError("domain must be non-empty and contain no whitespace")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    return DeployPlan(
        request=request,
        iac_ref=select_iac_ref(request.deploy_type, repo_root=repo_root, runner=runner),
        domain=effective_domain,
        timeout=timeout,
        newest_iac_tag=(
            newest_release_tag(repo_root=repo_root, runner=runner)
            if request.deploy_type in FIXED_DEPLOY_TYPES
            else ""
        ),
    )


def check_request_age(
    request: DeployRequest, *, created_at: str, now: datetime
) -> float | None:
    """Refuse a production request whose deploy can end after its sender stops (#1232).

    ``created_at`` is the first creation time of the receiver run, from the GitHub API.
    A re-run does not change it, so a re-run cannot make an old request new. Returns
    the age in seconds. Returns None for a request that is not production: a staging
    request can wait in the queue, and it has no age limit.
    """
    if request.deploy_type != DeployType.PRODUCTION:
        return None
    created = _utc_time(created_at)
    if now.tzinfo is None:
        raise ValueError("the current time must have a time zone")
    current = now.astimezone(timezone.utc)
    age = (current - created).total_seconds()
    if age < 0:
        raise ValueError(
            f"request age is negative: created {_iso(created)}, now {_iso(current)}. "
            "The runner clock or the creation time is wrong. Dispatch the release again."
        )
    if age > PRODUCTION_REQUEST_MAX_AGE_SECONDS:
        raise ValueError(
            f"request expired: created {_iso(created)}, now {_iso(current)}, "
            f"limit {PRODUCTION_REQUEST_MAX_AGE_SECONDS} s. A sender may stop waiting "
            "before this deploy ends, and nothing would verify the deploy. Dispatch the "
            "release again."
        )
    return age


def _utc_time(text: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise ValueError(
            f"request creation time {text!r} is not an ISO 8601 time with a time zone. "
            "A production request needs the created_at of its receiver run."
        )
    return parsed.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_evidence_urls(request: DeployRequest) -> None:
    repository_path = f"/{request.source_repository}/"
    _require_github_path(
        request.evidence.source_run_url,
        prefix=f"{repository_path}actions/runs/",
        field="source_run_url",
    )
    if request.deploy_type == DeployType.PRODUCTION:
        _require_github_path(
            request.evidence.staging_run_url,
            prefix=f"{repository_path}actions/runs/",
            field="staging_run_url",
        )
        _require_github_path(
            request.evidence.reviewed_change_url,
            prefix=f"{repository_path}pull/",
            field="reviewed_change_url",
        )


def _require_github_path(url: str, *, prefix: str, field: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or not parsed.path.startswith(prefix)
    ):
        raise ValueError(f"evidence.{field} must point to {prefix} on github.com")


def _git_url(repository: str) -> str:
    return f"https://github.com/{repository}.git"
