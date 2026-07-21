"""Fail-closed planning for cross-repository application deploy requests."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from infra2_sdk.deploy import (
    PRODUCTION_EVIDENCE_POLICY_PATH,
    DeployOperation,
    DeployRequest,
    DeployType,
    ProductionEvidencePolicy,
)
from infra2_sdk.refs import ResolvedRef, resolve_image_ref, resolve_pr

from libs.service_registry import domain_for_service

APP_SOURCES: dict[str, str] = {
    "finance_report/app": "wangzitian0/finance_report",
    "truealpha/app": "wangzitian0/truealpha",
}
ALLOWED_SENDERS = frozenset({"wangzitian0"})
FIXED_DEPLOY_TYPES = frozenset({DeployType.STAGING, DeployType.PRODUCTION})
_SEMVER_TAG_RE = re.compile(r"\Av[0-9]+\.[0-9]+\.[0-9]+\Z")
_GITHUB_API_URL = "https://api.github.com"
_GITHUB_API_VERSION = "2022-11-28"


def fetch_production_evidence_policy(
    request: DeployRequest,
    *,
    fetch_json: Callable[[str], Mapping[str, object]],
) -> ProductionEvidencePolicy:
    """Fetch the app's OWN evidence contract, pinned to the release being verified.

    #576: each app repo is the sole authority on its own CI facts, declared as a
    checked-in file at the SDK's canonical PRODUCTION_EVIDENCE_POLICY_PATH —
    fetched here at ``request.source_sha`` so the contract and the workflows it
    describes come from the SAME commit. There is deliberately NO fallback dict:
    a missing or malformed contract fails closed, loudly, naming the app and the
    expected path — an app without a contract is explicitly staging-only, never
    silently so (the infra2#571 detection gap).
    """
    where = (
        f"{request.source_repository}:{PRODUCTION_EVIDENCE_POLICY_PATH}"
        f"@{request.source_sha}"
    )
    try:
        payload = fetch_json(
            f"/repos/{request.source_repository}/contents/"
            f"{PRODUCTION_EVIDENCE_POLICY_PATH}?ref={request.source_sha}"
        )
    except (httpx.HTTPStatusError, ValueError, KeyError) as exc:
        # _fetch_github_json wraps HTTP failures (including a 404 for a repo
        # without the file) as ValueError; injected test fetchers may raise
        # httpx errors or KeyError directly.
        raise ValueError(
            f"service {request.service!r} has no Production evidence contract: "
            f"fetching {where} failed ({exc}). Production releases require the "
            "app repo to declare its own contract file (infra2#576); without one "
            "the app is staging-only."
        ) from exc
    content = payload.get("content")
    if not isinstance(content, str):
        raise ValueError(f"{where} did not return file content")
    try:
        raw = json.loads(base64.b64decode(content, validate=False))
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{where} is not valid JSON: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where} must contain a JSON object")
    try:
        policy = ProductionEvidencePolicy.from_dict(raw)
    except ValueError as exc:
        raise ValueError(f"{where} is not a valid evidence contract: {exc}") from exc
    if policy.service != request.service:
        raise ValueError(
            f"{where} declares service {policy.service!r}, not {request.service!r}"
        )
    return policy


@dataclass(frozen=True)
class DeployPlan:
    request: DeployRequest
    iac_ref: str
    domain: str
    timeout: int

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

    def to_dict(self) -> dict[str, object]:
        return {
            "request": self.request.to_dict(),
            "iac_ref": self.iac_ref,
            "domain": self.domain,
            "timeout": self.timeout,
            "deploy_v2_args": self.deploy_v2_args(),
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


def verify_production_evidence(
    request: DeployRequest,
    *,
    fetch_json: Callable[[str], Mapping[str, object]] | None = None,
) -> None:
    """Verify production evidence against GitHub's read-only API.

    The per-app expectations (workflow paths, events, display-title templates)
    come from the app's OWN checked-in contract file — fetched at the release's
    source_sha — never from an infra2-side dict (#576: the hardcoded
    PRODUCTION_EVIDENCE_POLICIES that drifted into infra2#571's five blockers
    is deleted, with no silent fallback).
    """
    fetch = fetch_json or _fetch_github_json
    # Pure-local URL-shape validation first (fail fast, no network), then the
    # app's own contract, then the evidence runs it describes.
    source_run_id = _github_evidence_number(
        request.evidence.source_run_url,
        repository=request.source_repository,
        resource="actions/runs",
        field="source_run_url",
    )
    if source_run_id != request.evidence.source_run_id:
        raise ValueError("evidence.source_run_id must match source_run_url")
    staging_run_id = _github_evidence_number(
        request.evidence.staging_run_url,
        repository=request.source_repository,
        resource="actions/runs",
        field="staging_run_url",
    )
    pull_number = _github_evidence_number(
        request.evidence.reviewed_change_url,
        repository=request.source_repository,
        resource="pull",
        field="reviewed_change_url",
    )
    policy = fetch_production_evidence_policy(request, fetch_json=fetch)

    source_run = fetch(
        f"/repos/{request.source_repository}/actions/runs/{source_run_id}"
    )
    staging_run = fetch(
        f"/repos/{request.source_repository}/actions/runs/{staging_run_id}"
    )
    reviewed_pull = fetch(f"/repos/{request.source_repository}/pulls/{pull_number}")
    _verify_run(
        source_run,
        label="source run",
        repository=request.source_repository,
        url=request.evidence.source_run_url,
        sha=request.source_sha if policy.source.require_head_sha else None,
        event=policy.source.event,
        workflow_path=policy.source.workflow_path,
        display_title=policy.source.expected_display_title(request.version_ref),
    )
    _verify_run(
        staging_run,
        label="staging run",
        repository=request.source_repository,
        url=request.evidence.staging_run_url,
        sha=request.source_sha if policy.staging.require_head_sha else None,
        event=policy.staging.event,
        workflow_path=policy.staging.workflow_path,
        display_title=policy.staging.expected_display_title(request.version_ref),
    )
    _verify_reviewed_pull(
        reviewed_pull,
        repository=request.source_repository,
        url=request.evidence.reviewed_change_url,
        sha=request.source_sha,
        base_ref=policy.review_base_ref,
    )


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
    result = runner(
        ["git", "tag", "--merged", "HEAD", "--sort=-version:refname"],
        cwd=Path(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    for tag in result.stdout.splitlines():
        cleaned = tag.strip()
        if _SEMVER_TAG_RE.match(cleaned):
            return cleaned
    raise ValueError("no released infra2 vX.Y.Z tag is merged into HEAD")


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
    if not effective_domain or any(character.isspace() for character in effective_domain):
        raise ValueError("domain must be non-empty and contain no whitespace")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    return DeployPlan(
        request=request,
        iac_ref=select_iac_ref(request.deploy_type, repo_root=repo_root, runner=runner),
        domain=effective_domain,
        timeout=timeout,
    )


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


def _github_evidence_number(
    url: str,
    *,
    repository: str,
    resource: str,
    field: str,
) -> str:
    parsed = urlparse(url)
    prefix = f"/{repository}/{resource}/"
    number = parsed.path.removeprefix(prefix)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or not parsed.path.startswith(prefix)
        or not number.isdigit()
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"evidence.{field} must be a canonical github.com {resource} URL"
        )
    return number


def _fetch_github_json(path: str) -> Mapping[str, object]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "infra2-app-deploy-receiver",
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
    }
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = httpx.get(
            f"{_GITHUB_API_URL}{path}",
            headers=headers,
            follow_redirects=False,
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise ValueError(
            f"GitHub evidence request failed for {path}: HTTP {exc.response.status_code}"
        ) from None
    except (httpx.RequestError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"GitHub evidence request failed for {path}: {type(exc).__name__}"
        ) from None
    if not isinstance(payload, Mapping):
        raise ValueError(f"GitHub evidence response for {path} must be an object")
    return payload


def _verify_run(
    run: Mapping[str, object],
    *,
    label: str,
    repository: str,
    url: str,
    sha: str | None,
    event: str,
    workflow_path: str,
    display_title: str,
) -> None:
    """``sha=None`` means the run's expectation declared require_head_sha=false
    (a branch-dispatched staging run: its head_sha is the branch tip at dispatch
    time, not the release commit — the version linkage is the display title's
    version_ref, plus the receiver's own version_ref->sha pin at execution time).
    Every other check below is unconditional."""
    remote_repository = run.get("repository")
    if (
        not isinstance(remote_repository, Mapping)
        or remote_repository.get("full_name") != repository
    ):
        raise ValueError(f"{label} repository does not match source_repository")
    if run.get("html_url") != url:
        raise ValueError(f"{label} html_url does not match submitted evidence")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise ValueError(f"{label} must be completed successfully")
    if sha is not None and run.get("head_sha") != sha:
        raise ValueError(f"{label} head_sha does not match source_sha")
    if run.get("path") != workflow_path:
        raise ValueError(f"{label} workflow is not approved for Production evidence")
    if run.get("event") != event:
        raise ValueError(f"{label} event does not match the evidence policy")
    if run.get("display_title") != display_title:
        raise ValueError(f"{label} title does not match the requested version_ref")


def _verify_reviewed_pull(
    pull: Mapping[str, object],
    *,
    repository: str,
    url: str,
    sha: str,
    base_ref: str,
) -> None:
    base = pull.get("base")
    remote_repository = base.get("repo") if isinstance(base, Mapping) else None
    if (
        not isinstance(remote_repository, Mapping)
        or remote_repository.get("full_name") != repository
    ):
        raise ValueError(
            "reviewed pull request repository does not match source_repository"
        )
    if pull.get("html_url") != url:
        raise ValueError(
            "reviewed pull request html_url does not match submitted evidence"
        )
    if pull.get("state") != "closed" or not pull.get("merged_at"):
        raise ValueError("reviewed pull request must be merged")
    if not isinstance(base, Mapping) or base.get("ref") != base_ref:
        raise ValueError("reviewed pull request base branch is not approved")
    if pull.get("merge_commit_sha") != sha:
        raise ValueError(
            "reviewed pull request merge_commit_sha does not match source_sha"
        )


def _git_url(repository: str) -> str:
    return f"https://github.com/{repository}.git"
