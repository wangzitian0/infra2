"""Control-plane production deploy evidence verification (absorbed from infra2-sdk v4.9.1)."""

from __future__ import annotations

import binascii
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse

from infra2_sdk.deploy import DeployRequest, DeployType

from common.core._sdk_wire import (
    canonical_json_bytes,
    parse_contract_version,
    parse_string,
    require_contract_version,
)
from common.core.http_transport import (
    _decode_github_contents,
    _github_headers,
    _github_json_fetcher,
)

PRODUCTION_REQUEST_MAX_AGE_SECONDS = 24 * 60
PRODUCTION_DEPLOY_BUDGET_SECONDS = 300
VERSION_KEYS = ("git_sha", "version")
_SERVICE_RE = re.compile(r"\A[a-z][a-z0-9_]*/[a-z][a-z0-9_]*\Z")
_SHA40_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_HOST_RE = re.compile(r"\Ahttps?://github\.com\Z")
_GITHUB_NUMBER_TAIL_RE = re.compile(r"\A[1-9][0-9]*\Z")


_EVIDENCE_POLICY_CONTRACT_VERSION = 1
PRODUCTION_EVIDENCE_POLICY_PATH = "tools/production_evidence_policy.json"
_WORKFLOW_PATH_RE = re.compile(r"\A\.github/workflows/[A-Za-z0-9._-]+\.ya?ml\Z")
_TITLE_PLACEHOLDER = "{version_ref}"
_RUN_EVENTS = frozenset({"push", "workflow_dispatch"})


@dataclass(frozen=True)
class _RunEvidenceExpectation:
    workflow_path: str
    event: str
    display_title_template: str
    require_head_sha: bool = True

    def __post_init__(self) -> None:
        if not _WORKFLOW_PATH_RE.match(self.workflow_path):
            raise ValueError(
                "workflow_path must be a .github/workflows/<name>.yml path"
            )
        if self.event not in _RUN_EVENTS:
            raise ValueError(f"event must be one of {sorted(_RUN_EVENTS)}")
        if not self.display_title_template.strip():
            raise ValueError("display_title_template is required")
        residue = self.display_title_template.replace(_TITLE_PLACEHOLDER, "")
        if "{" in residue or "}" in residue:
            raise ValueError(
                "display_title_template supports the literal {version_ref} placeholder only"
            )

    def expected_display_title(self, version_ref: str) -> str:
        return self.display_title_template.replace(_TITLE_PLACEHOLDER, version_ref)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> _RunEvidenceExpectation:
        req_sha = raw.get("require_head_sha", True)
        if type(req_sha) is not bool:
            raise ValueError("require_head_sha must be a boolean")
        return cls(
            workflow_path=parse_string(raw, "workflow_path"),
            event=parse_string(raw, "event"),
            display_title_template=parse_string(raw, "display_title_template"),
            require_head_sha=req_sha,
        )


@dataclass(frozen=True)
class ProductionEvidencePolicy:
    service: str
    source: _RunEvidenceExpectation
    staging: _RunEvidenceExpectation
    review_base_ref: str
    contract_version: int = _EVIDENCE_POLICY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        require_contract_version(
            self.contract_version,
            _EVIDENCE_POLICY_CONTRACT_VERSION,
            description="evidence policy contract_version",
        )
        if not _SERVICE_RE.match(self.service):
            raise ValueError("service must have the form project/service")
        if not self.review_base_ref.strip():
            raise ValueError("review_base_ref is required")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ProductionEvidencePolicy:
        ver = parse_contract_version(
            raw,
            _EVIDENCE_POLICY_CONTRACT_VERSION,
            description="evidence policy contract_version",
        )
        src, stg = raw.get("source"), raw.get("staging")
        if not isinstance(src, Mapping):
            raise ValueError("source must be an object")
        if not isinstance(stg, Mapping):
            raise ValueError("staging must be an object")
        return cls(
            contract_version=ver,
            service=parse_string(raw, "service"),
            source=_RunEvidenceExpectation.from_dict(src),
            staging=_RunEvidenceExpectation.from_dict(stg),
            review_base_ref=parse_string(raw, "review_base_ref"),
        )


def _default_github_json_fetcher(
    *,
    token: str = "",
    token_env: str = "GITHUB_TOKEN",
    user_agent: str = "infra2-sdk",
    timeout: float = 10.0,
) -> Callable[[str], Any]:
    return _github_json_fetcher(
        headers=_github_headers(
            token=token or os.getenv(token_env, ""), user_agent=user_agent
        ),
        timeout=timeout,
        error_cls=ValueError,
        http_message="GitHub evidence request failed for {path}: HTTP {code}",
        error_message="GitHub evidence request failed for {path}: {error}",
        error_text=lambda exc: type(exc).__name__,
    )


def _require_github_path(url: str, *, prefix: str, field: str) -> None:
    p = urlparse(url)
    if p.scheme != "https" or p.netloc != "github.com" or not p.path.startswith(prefix):
        raise ValueError(f"evidence.{field} must point to {prefix} on github.com")


def _github_evidence_number(
    url: str, *, repository: str, resource: str, field: str
) -> str:
    p = urlparse(url)
    prefix = f"/{repository}/{resource}/"
    num = p.path.removeprefix(prefix)
    if (
        p.scheme != "https"
        or p.netloc != "github.com"
        or not p.path.startswith(prefix)
        or not num.isdigit()
        or p.params
        or p.query
        or p.fragment
    ):
        raise ValueError(
            f"evidence.{field} must be a canonical github.com {resource} URL"
        )
    return num


def validate_evidence_urls(
    request: DeployRequest, *, expected_repository: str | None = None
) -> None:
    if (
        expected_repository is not None
        and expected_repository != request.source_repository
    ):
        raise ValueError("source_repository does not match the expected repository")
    base = f"/{expected_repository or request.source_repository}/"
    _require_github_path(
        request.evidence.source_run_url,
        prefix=f"{base}actions/runs/",
        field="source_run_url",
    )
    if request.deploy_type == DeployType.PRODUCTION:
        _require_github_path(
            request.evidence.staging_run_url,
            prefix=f"{base}actions/runs/",
            field="staging_run_url",
        )
        _require_github_path(
            request.evidence.reviewed_change_url,
            prefix=f"{base}pull/",
            field="reviewed_change_url",
        )


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
    repo = run.get("repository")
    if not isinstance(repo, Mapping) or repo.get("full_name") != repository:
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
    reviews: Sequence[object] | None = None,
    repository: str,
    url: str,
    sha: str,
    base_ref: str,
) -> None:
    base = pull.get("base")
    repo = base.get("repo") if isinstance(base, Mapping) else None
    if not isinstance(repo, Mapping) or repo.get("full_name") != repository:
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
    if reviews is not None:
        user = pull.get("user")
        author = user.get("login") if isinstance(user, Mapping) else None
        states = {
            r["user"]["login"]: r["state"]
            for r in reviews
            if isinstance(r, Mapping)
            and isinstance(r.get("user"), Mapping)
            and isinstance(r["user"].get("login"), str)
            and r["user"]["login"]
            and isinstance(r.get("state"), str)
        }
        if author:
            states.pop(author, None)
        if any(s == "CHANGES_REQUESTED" for s in states.values()):
            raise ValueError("reviewed pull request has pending CHANGES_REQUESTED")


def _parse_evidence_policy_payload(
    where: str, payload: Any, *, service: str | None = None
) -> ProductionEvidencePolicy:
    try:
        raw = json.loads(_decode_github_contents(payload))
    except TypeError:
        raise ValueError(f"{where} must contain a JSON object") from None
    except KeyError:
        raise ValueError(f"{where} did not return file content") from None
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            f"malformed {PRODUCTION_EVIDENCE_POLICY_PATH}: not valid JSON ({exc})"
        ) from exc
    if not isinstance(raw, Mapping):
        raise ValueError(
            f"malformed {PRODUCTION_EVIDENCE_POLICY_PATH}: expected JSON object"
        )
    try:
        pol = ProductionEvidencePolicy.from_dict(raw)
    except ValueError as exc:
        raise ValueError(f"{where} is not a valid evidence contract: {exc}") from exc
    if service is not None and pol.service != service:
        raise ValueError(f"{where} declares service {pol.service!r}, not {service!r}")
    return pol


def _fetch_production_evidence_policy(
    request: DeployRequest, *, fetch_json: Callable[[str], Any] | None = None
) -> ProductionEvidencePolicy:
    where = f"{request.source_repository}:{PRODUCTION_EVIDENCE_POLICY_PATH}@{request.source_sha}"
    fetch = fetch_json or _default_github_json_fetcher()
    try:
        payload = fetch(
            f"/repos/{request.source_repository}/contents/"
            f"{PRODUCTION_EVIDENCE_POLICY_PATH}?ref={request.source_sha}"
        )
    except Exception as exc:
        raise ValueError(
            f"service {request.service!r} has no Production evidence contract: "
            f"fetching {where} failed ({exc})."
        ) from exc
    return _parse_evidence_policy_payload(where, payload, service=request.service)


def verify_production_evidence(
    request: DeployRequest,
    policy: ProductionEvidencePolicy | None = None,
    *,
    fetch_json: Callable[[str], Any] | None = None,
) -> None:
    fetch = fetch_json or _default_github_json_fetcher()
    validate_evidence_urls(request)
    repo = request.source_repository
    src_id = _github_evidence_number(
        request.evidence.source_run_url,
        repository=repo,
        resource="actions/runs",
        field="source_run_url",
    )
    if src_id != request.evidence.source_run_id:
        raise ValueError("evidence.source_run_id must match source_run_url")
    stg_id = _github_evidence_number(
        request.evidence.staging_run_url,
        repository=repo,
        resource="actions/runs",
        field="staging_run_url",
    )
    pr_num = _github_evidence_number(
        request.evidence.reviewed_change_url,
        repository=repo,
        resource="pull",
        field="reviewed_change_url",
    )
    pol = policy or _fetch_production_evidence_policy(request, fetch_json=fetch)
    src_run = fetch(f"/repos/{repo}/actions/runs/{src_id}")
    stg_run = fetch(f"/repos/{repo}/actions/runs/{stg_id}")
    pull = fetch(f"/repos/{repo}/pulls/{pr_num}")
    reviews = fetch(f"/repos/{repo}/pulls/{pr_num}/reviews")
    if not isinstance(reviews, Sequence) or isinstance(reviews, (str, bytes, Mapping)):
        raise ValueError(
            f"GitHub evidence response for /repos/{repo}/pulls/{pr_num}/reviews must be a list"
        )
    for run, label, url, exp in (
        (src_run, "source run", request.evidence.source_run_url, pol.source),
        (stg_run, "staging run", request.evidence.staging_run_url, pol.staging),
    ):
        _verify_run(
            run,
            label=label,
            repository=repo,
            url=url,
            sha=request.source_sha if exp.require_head_sha else None,
            event=exp.event,
            workflow_path=exp.workflow_path,
            display_title=exp.expected_display_title(request.version_ref),
        )
    _verify_reviewed_pull(
        pull,
        reviews=reviews,
        repository=repo,
        url=request.evidence.reviewed_change_url,
        sha=request.source_sha,
        base_ref=pol.review_base_ref,
    )


__all__ = [
    "PRODUCTION_DEPLOY_BUDGET_SECONDS",
    "PRODUCTION_EVIDENCE_POLICY_PATH",
    "PRODUCTION_REQUEST_MAX_AGE_SECONDS",
    "ProductionEvidencePolicy",
    "VERSION_KEYS",
    "canonical_json_bytes",
    "validate_evidence_urls",
    "verify_production_evidence",
]
