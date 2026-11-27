"""Control-plane production deploy evidence verification (absorbed from infra2-sdk)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import time
from typing import Any

import httpx

from common.core._sdk_wire import (
    canonical_json_bytes,
    parse_contract_version,
    parse_iso_utc,
    parse_string,
    require_contract_version,
)

CONTRACT_VERSION = 1
PRODUCTION_REQUEST_MAX_AGE_SECONDS = 24 * 60
PRODUCTION_DEPLOY_BUDGET_SECONDS = 300
SENDER_MAX_WAIT_SECONDS = 6 * 60 * 60
SENDER_DEADLINE_KEY = "sender_deadline"
VERSION_KEYS = ("git_sha", "version")
_REQUEST_ID_RE = re.compile(r"\A[a-zA-Z0-9][a-zA-Z0-9._:-]{7,127}\Z")
_SERVICE_RE = re.compile(r"\A[a-z][a-z0-9_]*/[a-z][a-z0-9_]*\Z")
_REPOSITORY_RE = re.compile(r"\A[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+\Z")
_SHA40_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_HOST_RE = re.compile(r"\Ahttps?://github\.com\Z")
_GITHUB_NUMBER_TAIL_RE = re.compile(r"\A[1-9][0-9]*\Z")


class DeployOperation(StrEnum):
    DEPLOY = "deploy"
    ROLLBACK = "rollback"
    REMOVE = "remove"


class DeployType(StrEnum):
    STAGING = "staging"
    PRODUCTION = "prod"
    PREVIEW_BRANCH = "preview/branch"
    PREVIEW_PR = "preview/pr"
    PREVIEW_COMMIT = "preview/commit"
    PREVIEW_TAG = "preview/tag"
    CANARY = "canary"


def parse_sender_deadline(payload: Mapping[str, Any]) -> datetime | None:
    val = payload.get(SENDER_DEADLINE_KEY)
    if val is None:
        return None
    if not isinstance(val, str):
        raise ValueError(f"{SENDER_DEADLINE_KEY} must be an ISO 8601 string")
    try:
        return parse_iso_utc(val)
    except ValueError:
        raise ValueError(
            f"{SENDER_DEADLINE_KEY} {val!r} is not an ISO 8601 time with a time zone"
        ) from None


def production_request_accept_until(
    created: datetime, sender_deadline: datetime | None
) -> datetime:
    if created.tzinfo is None:
        raise ValueError("the creation time must have a time zone")
    created = created.astimezone(UTC)
    if sender_deadline is None:
        return created + timedelta(seconds=PRODUCTION_REQUEST_MAX_AGE_SECONDS)
    if sender_deadline.tzinfo is None:
        raise ValueError("the sender deadline must have a time zone")
    latest = created + timedelta(seconds=SENDER_MAX_WAIT_SECONDS)
    return min(sender_deadline.astimezone(UTC), latest) - timedelta(
        seconds=PRODUCTION_DEPLOY_BUDGET_SECONDS
    )


@dataclass(frozen=True)
class DeployEvidence:
    source_run_url: str
    source_run_id: str = ""
    staging_run_url: str = ""
    reviewed_change_url: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DeployEvidence:
        return cls(
            source_run_url=parse_string(raw, "source_run_url"),
            source_run_id=parse_string(raw, "source_run_id", required=False),
            staging_run_url=parse_string(raw, "staging_run_url", required=False),
            reviewed_change_url=parse_string(
                raw, "reviewed_change_url", required=False
            ),
        )


@dataclass(frozen=True)
class DeployRequest:
    request_id: str
    operation: DeployOperation
    service: str
    deploy_type: DeployType
    version_ref: str
    source_repository: str
    source_sha: str
    evidence: DeployEvidence
    contract_version: int = CONTRACT_VERSION

    def __post_init__(self) -> None:
        _validate_deploy_request(self)

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["operation"] = self.operation.value
        data["deploy_type"] = self.deploy_type.value
        return data

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DeployRequest:
        ev = raw.get("evidence")
        if not isinstance(ev, Mapping):
            raise ValueError("evidence must be an object")
        ver = parse_contract_version(
            raw, CONTRACT_VERSION, description="contract_version"
        )
        return cls(
            contract_version=ver,
            request_id=parse_string(raw, "request_id"),
            operation=DeployOperation(parse_string(raw, "operation")),
            service=parse_string(raw, "service"),
            deploy_type=DeployType(parse_string(raw, "deploy_type")),
            version_ref=parse_string(raw, "version_ref"),
            source_repository=parse_string(raw, "source_repository"),
            source_sha=parse_string(raw, "source_sha").lower(),
            evidence=DeployEvidence.from_dict(ev),
        )


def _validate_deploy_request(req: DeployRequest) -> None:
    require_contract_version(
        req.contract_version, CONTRACT_VERSION, description="contract_version"
    )
    if not _REQUEST_ID_RE.match(req.request_id):
        raise ValueError("request_id must be 8-128 URL-safe characters")
    if not _SERVICE_RE.match(req.service):
        raise ValueError("service must have the form project/service")
    if not req.version_ref.strip():
        raise ValueError("version_ref is required")
    if not _REPOSITORY_RE.match(req.source_repository):
        raise ValueError("source_repository must have the form owner/repository")
    if not _SHA40_RE.match(req.source_sha):
        raise ValueError("source_sha must be a lowercase 40-hex commit sha")
    if not req.evidence.source_run_url.startswith("https://github.com/"):
        raise ValueError("evidence.source_run_url must be a GitHub URL")
    if req.deploy_type == DeployType.PRODUCTION and not (
        req.evidence.staging_run_url and req.evidence.reviewed_change_url
    ):
        raise ValueError(
            "production deploys require staging and reviewed-change evidence"
        )
    if req.operation == DeployOperation.REMOVE and req.deploy_type not in {
        DeployType.PREVIEW_BRANCH,
        DeployType.PREVIEW_PR,
        DeployType.PREVIEW_COMMIT,
        DeployType.PREVIEW_TAG,
        DeployType.CANARY,
    }:
        raise ValueError("remove is limited to preview and canary targets")


HttpGet = Callable[[str], tuple[int, str]]


@dataclass(frozen=True)
class HealthCheckResult:
    attempts: int
    status_code: int
    body: str


def default_http_get(
    *, timeout: float = 10.0, follow_redirects: bool = True
) -> HttpGet:
    def http_get(url: str) -> tuple[int, str]:
        try:
            resp = httpx.get(url, timeout=timeout, follow_redirects=follow_redirects)
        except httpx.HTTPError as exc:
            return 0, str(exc)
        return resp.status_code, resp.text

    return http_get


def _maybe_sleep(
    sleep: Callable[[float], None], interval: float, attempt: int, max_attempts: int
) -> None:
    if attempt < max_attempts:
        sleep(interval)


def _parse_json_object(body: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _first_present(parsed: dict[str, Any], keys: tuple[str, ...]) -> str:
    return next((v for k in keys if isinstance(v := parsed.get(k), str) and v), "")


def _version_prefix_matches(actual: str, expected: str) -> bool:
    return bool(actual) and (actual.startswith(expected) or expected.startswith(actual))


def poll_until_healthy(
    url: str,
    *,
    http_get: HttpGet,
    expected_version: str = "",
    version_json_keys: tuple[str, ...] = ("git_sha", "version"),
    require_status: str | None = None,
    max_attempts: int = 24,
    max_version_mismatch_attempts: int | None = None,
    interval_seconds: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
) -> HealthCheckResult:
    budget = max_version_mismatch_attempts or max_attempts
    streak, last_mismatch, last_status = 0, "", 0
    for attempt in range(1, max_attempts + 1):
        status_code, body = http_get(url)
        last_status = status_code
        if status_code != 200:
            _maybe_sleep(sleep, interval_seconds, attempt, max_attempts)
            continue
        parsed = _parse_json_object(body)
        if require_status is not None and (
            not parsed or parsed.get("status") != require_status
        ):
            _maybe_sleep(sleep, interval_seconds, attempt, max_attempts)
            continue
        if expected_version:
            actual = _first_present(parsed, version_json_keys) if parsed else ""
            if not _version_prefix_matches(actual, expected_version):
                streak = streak + 1 if actual == last_mismatch else 1
                last_mismatch = actual
                if streak >= budget:
                    raise RuntimeError(
                        f"{url}: still reporting version {actual!r} (expected "
                        f"{expected_version!r} or a prefix match) after {streak} stable mismatches"
                    )
                _maybe_sleep(sleep, interval_seconds, attempt, max_attempts)
                continue
        return HealthCheckResult(attempts=attempt, status_code=status_code, body=body)
    raise RuntimeError(
        f"{url}: did not become healthy after {max_attempts} attempts "
        f"(last status: HTTP {last_status})"
    )


__all__ = [
    "CONTRACT_VERSION",
    "DeployEvidence",
    "DeployOperation",
    "DeployRequest",
    "DeployType",
    "HealthCheckResult",
    "HttpGet",
    "PRODUCTION_DEPLOY_BUDGET_SECONDS",
    "PRODUCTION_REQUEST_MAX_AGE_SECONDS",
    "SENDER_DEADLINE_KEY",
    "SENDER_MAX_WAIT_SECONDS",
    "VERSION_KEYS",
    "canonical_json_bytes",
    "default_http_get",
    "parse_sender_deadline",
    "poll_until_healthy",
    "production_request_accept_until",
]
