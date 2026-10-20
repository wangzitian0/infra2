"""Fail-closed planning for cross-repository application deploy requests."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from infra2_sdk.deploy import (
    PRODUCTION_REQUEST_MAX_AGE_SECONDS,
    DeployOperation,
    DeployRequest,
    DeployType,
    validate_evidence_urls,
    verify_production_evidence,
)
from infra2_sdk.refs import (
    ResolvedRef,
    resolve_image_ref,
    resolve_pr,
)

from common.core.constants import APP_SOURCES
from common.core.fetch_app_manifests import LOCK as APP_MANIFEST_LOCK, http_get
from common.core.registry import domain_for_service
from common.deploy.release_markers import (
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
    "ServingRelease",
    "check_request_age",
    "make_plan",
    "marker_status",
    "newest_release_tag",
    "parse_request",
    "production_marker",
    "query_serving_release",
    "select_iac_ref",
    "validate_request_authority",
    "verify_app_manifests",
    "verify_production_evidence",
]

ALLOWED_SENDERS = frozenset({"wangzitian0"})
FIXED_DEPLOY_TYPES = frozenset({DeployType.STAGING, DeployType.PRODUCTION})
#: The deploy types whose request is canaried before it executes (truealpha#860):
#: production only. app-deploy-request.yml's preflight_canary pre-filter mirrors this set;
#: common/tests/test_app_deploy_request_workflow.py pins the two to each other and the
#: receiver re-checks it at run time, so they cannot drift apart silently.
PREFLIGHT_CANARY_DEPLOY_TYPES = frozenset({DeployType.PRODUCTION})


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

    validate_evidence_urls(request, expected_repository=expected_source)
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


def _load_app_manifest_lock(
    *,
    repo_root: str | Path,
    iac_ref: str,
    runner=subprocess.run,
) -> dict[str, object] | None:
    root = Path(repo_root)
    lock_file = root / APP_MANIFEST_LOCK
    if iac_ref and iac_ref != "main":
        try:
            proc = runner(
                ["git", "-C", str(root), "show", f"{iac_ref}:{APP_MANIFEST_LOCK}"],
                capture_output=True,
                text=True,
                check=True,
            )
            if isinstance(proc.stdout, str) and proc.stdout.strip().startswith("{"):
                return json.loads(proc.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    if lock_file.is_file():
        return json.loads(lock_file.read_text(encoding="utf-8"))
    return None


def verify_app_manifests(
    request: DeployRequest,
    *,
    repo_root: str | Path,
    iac_ref: str = "main",
    manifest_fetcher: Callable[[str], bytes] | None = None,
    runner=subprocess.run,
) -> None:
    """Compare the sha256 of each manifest of the requested release SHA with the lock (#1297)."""
    if request.operation == DeployOperation.REMOVE:
        return
    lock = _load_app_manifest_lock(repo_root=repo_root, iac_ref=iac_ref, runner=runner)
    if lock is None:
        if manifest_fetcher is None:
            return
        raise ValueError(f"{APP_MANIFEST_LOCK} is missing under {repo_root}")
    app_name = request.service.split("/", 1)[0]
    files = [
        f
        for f in lock.get("files", [])  # type: ignore[union-attr]
        if isinstance(f, Mapping) and f.get("app") == app_name
    ]
    if not files:
        return
    fetch = manifest_fetcher or http_get
    differing: list[str] = []
    for entry in files:
        path = str(entry["path"])
        upstream = str(entry["upstream"])
        expected_sha256 = str(entry["sha256"])
        url = (
            f"https://raw.githubusercontent.com/"
            f"{request.source_repository}/{request.source_sha}/{upstream}"
        )
        try:
            body = fetch(url)
        except SystemExit as exc:
            raise ValueError(f"failed to fetch manifest {upstream} from {url}: {exc}") from None
        actual_sha256 = hashlib.sha256(body).hexdigest() if body else ""
        if not body or actual_sha256 != expected_sha256:
            differing.append(path)
    if differing:
        joined = ", ".join(differing)
        raise ValueError(
            f"release manifests at {request.source_sha[:12]} differ from {APP_MANIFEST_LOCK} "
            f"({joined}): run `tools/fetch_app_manifests.py --sync {app_name} {request.version_ref}` "
            "and `tools/secrets_render.py --write`, merge, then deploy again"
        )


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
    manifest_fetcher: Callable[[str], bytes] | None = None,
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
    iac_ref = select_iac_ref(request.deploy_type, repo_root=repo_root, runner=runner)
    verify_app_manifests(
        request,
        repo_root=repo_root,
        iac_ref=iac_ref,
        manifest_fetcher=manifest_fetcher,
        runner=runner,
    )
    return DeployPlan(
        request=request,
        iac_ref=iac_ref,
        domain=effective_domain,
        timeout=timeout,
        newest_iac_tag=(
            newest_release_tag(repo_root=repo_root, runner=runner)
            if request.deploy_type in FIXED_DEPLOY_TYPES
            else ""
        ),
    )


def check_request_age(
    request: DeployRequest, *, created_at: str, run_attempt: str, now: datetime
) -> float | None:
    """Refuse a production request whose deploy can end after its sender stops (#1232).

    ``created_at`` is the first creation time of the receiver run, from the GitHub API.
    A re-run does not change it. ``run_attempt`` is GITHUB_RUN_ATTEMPT: the sender
    stops when the first attempt ends, so a production re-run is refused. Every
    request parses ``created_at``, so staging deploys use the read and the parse
    before the first production deploy does. Returns the age in seconds. Returns
    None for a request that is not production: a staging request can wait in the
    queue, and it has no age limit and no re-run rule.
    """
    created = _utc_time(created_at)
    if request.deploy_type != DeployType.PRODUCTION:
        return None
    attempt = _run_attempt(run_attempt)
    if attempt != 1:
        raise ValueError(
            f"request is a re-run (attempt {attempt}). The sender does not wait for a "
            "re-run, and nothing would verify the deploy. Dispatch the release again."
        )
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
            "Every request needs the created_at of its receiver run."
        )
    return parsed.astimezone(timezone.utc)


def _run_attempt(text: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]*", text):
        raise ValueError(
            f"run attempt {text!r} is not a positive integer. A production request "
            "needs the GITHUB_RUN_ATTEMPT of its receiver run."
        )
    return int(text)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_url(repository: str) -> str:
    return f"https://github.com/{repository}.git"


@dataclass(frozen=True)
class ServingRelease:
    """Authoritative deployed/serving state for a fixed app environment (#588)."""

    service: str
    environment: str
    compose_id: str
    image_tag: str
    source_sha: str
    iac_ref: str
    public_url: str
    public_version: str
    healthy: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "service": self.service,
            "environment": self.environment,
            "compose_id": self.compose_id,
            "image_tag": self.image_tag,
            "source_sha": self.source_sha,
            "iac_ref": self.iac_ref,
            "public_url": self.public_url,
            "public_version": self.public_version,
            "healthy": self.healthy,
        }


def query_serving_release(
    service: str,
    environment: str,
    *,
    domain: str,
    client: object | None = None,
    http_get_fn: Callable[[str], tuple[int, str]] | None = None,
    resolve_image: Callable[..., ResolvedRef] = resolve_image_ref,
) -> ServingRelease:
    """Query infra2's authoritative serving state for ``(service, environment)`` (#588).

    App release workflows consume this instead of re-deriving deployment/rollback state
    from their own GitHub Actions run history. Reads Dokploy compose env (`IMAGE_TAG`,
    `GIT_COMMIT_SHA`, `INFRA_IAC_REF`) when a Dokploy client is available and cross-checks
    the live `/api/health` endpoint, then resolves the serving release ref against the
    service's repository to return both `image_tag` and 40-hex `source_sha`.
    """
    from common.deploy.env_config import app_compose_env_config
    from common.deploy.env_util import _parse_env_text
    from infra2_sdk.deploy import default_http_get

    expected_source = APP_SOURCES.get(service)
    if expected_source is None:
        raise ValueError(f"service {service!r} is not an app service with serving state")
    norm_env = "prod" if environment in ("prod", "production") else environment
    if norm_env not in ("staging", "prod"):
        raise ValueError(
            f"serving state query supports fixed environments ('staging' or 'prod'), got {environment!r}"
        )
    effective_domain = domain_for_service(service) or domain
    if not effective_domain or any(c.isspace() for c in effective_domain):
        raise ValueError("domain must be non-empty and contain no whitespace")
    cfg = app_compose_env_config(service, norm_env)
    compose_id = cfg.compose_id or ""
    public_url = f"{cfg.app_url(domain=effective_domain).rstrip('/')}/api/health"

    env_map: dict[str, str] = {}
    if client is not None and compose_id and hasattr(client, "get_compose_env"):
        env_text = client.get_compose_env(compose_id) or ""
        env_map = _parse_env_text(env_text)

    getter = http_get_fn or default_http_get()
    public_version = ""
    healthy = False
    try:
        status_code, body = getter(public_url)
        if status_code == 200 and body:
            parsed = json.loads(body)
            if isinstance(parsed, Mapping):
                healthy = str(parsed.get("status") or "ok").lower() in (
                    "ok",
                    "healthy",
                )
                for key in ("version", "git_sha"):
                    val = parsed.get(key)
                    if isinstance(val, str) and val.strip():
                        public_version = val.strip()
                        break
    except (OSError, ValueError):
        healthy = False

    image_tag = (
        env_map.get("IMAGE_TAG")
        or env_map.get("INFRA_SERVICE_VERSION")
        or public_version
    )
    if not image_tag:
        raise RuntimeError(
            f"cannot determine serving release for {service} ({norm_env}): "
            f"neither Dokploy compose {compose_id!r} nor {public_url} reported a version"
        )
    iac_ref = env_map.get("INFRA_IAC_REF", "")
    source_sha = ""
    raw_sha = env_map.get("GIT_COMMIT_SHA", "")
    if re.fullmatch(r"[0-9a-f]{40}", raw_sha):
        source_sha = raw_sha
    else:
        try:
            resolved = resolve_image(image_tag, repo=_git_url(expected_source))
            source_sha = resolved.sha.lower()
        except Exception:  # noqa: BLE001
            source_sha = raw_sha
    return ServingRelease(
        service=service,
        environment="production" if norm_env == "prod" else "staging",
        compose_id=compose_id,
        image_tag=image_tag,
        source_sha=source_sha,
        iac_ref=iac_ref,
        public_url=public_url,
        public_version=public_version or image_tag,
        healthy=healthy or bool(env_map.get("IMAGE_TAG")),
    )
