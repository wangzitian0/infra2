#!/usr/bin/env python3
"""Reconcile IaC-pinned services whose deploy inputs changed between release tags.

This is the release-tag promotion layer for input-drift reconciliation: diff the
previous release tag -> the promoted tag -> deploy dependency fan-out -> deploy_v2/
iac_runner per affected service (pinned to the tag) -> Deployer config-hash gate decides
no-op vs restart. staging/prod accept tags only, so the promoted ref is always a tag.
"""

from __future__ import annotations

import argparse
import ast
import concurrent.futures
from collections.abc import Mapping
from contextlib import contextmanager
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence
import yaml

from common.core.constants import (
    APP_REPO_ID_TO_SERVICE,
    APP_SOURCES,
    REPO_ROOT,
    SERVICE_TO_SUBMODULE_PATH,
)
from common.core.registry import domain_for_service, service_attrs
from common.deploy.app_contract import (
    APP_HOOKS_MANIFEST_PATH,
    HOOK_RUNNER_CONTEXT_ENV,
    HOOK_RUNNER_SECRET_NAMES,
    HOOK_SLOTS,
    RuntimeHooksManifest,
    execute_app_hook_slot,
    hook_env_for_slot,
    fetch_hooks_manifest,
    git_url,
    load_hooks_manifest,
    next_app_release_tag,
    query_serving_release,
    validate_app_release_evidence,
)
from common.deploy.dependencies import explain_fanout
from common.deploy.deploy_guard_audit import app_hook_pair
from common.deploy.transient_failure import classify_failure
from common.deploy.contract import all_service_keys, service_spec, validate_iac_ref_form
from common.deploy.preflight import (
    assert_app_version_ref_matches_train,
    assert_app_version_ref_on_main,
)
from common.deploy.env_config import (
    app_compose_env_config,
    preview_alias,
    preview_service_config,
)
from common.deploy.stage_record import CI_ENV, sdk_unit, utc_now, write_stage_record
from common.deploy.stage_record_reader import (
    EXIT_UNREADABLE,
    read_record,
    staging_gates_verdict,
)
from common.deploy.release_markers import (
    PRODUCTION_MARKER_PREFIX,
    RELEASE_TAG_RE,
    newest_release_tag,
    version_key,
)
from common.observability.issue_trail import CheckVerdict
from infra2_sdk.deploy import DeployRequest
from infra2_sdk.manifests import (
    FORBIDDEN_HOOK_ENV_NAMES,
    FORBIDDEN_HOOK_ENV_PREFIXES,
    STAGE_HOOK_PLANS,  # noqa: F401
    HookRunResult,
    StageHookPlan,  # noqa: F401
    stage_hook_plan,
)
from infra2_sdk.stage_record import StageRecord
from infra2_sdk.refs import (
    assert_after_on_main,  # noqa: F401  (re-exported; #616)
    classify_ref,
    next_patch_tag,
    resolve_image_ref,
)

MANIFEST_PATH = "docs/ssot/deploy-dependencies.yaml"
HARNESS_REPOS_PATH = "harness/repos.yaml"
SERVICE_TO_APP_REPO_ID: dict[str, str] = {
    service: repo_id for repo_id, service in APP_REPO_ID_TO_SERVICE.items()
}


def _stage_slots_csv(stage: str) -> str:
    """Return comma-separated deploy+post hook slots for ``stage`` from ``STAGE_HOOK_PLANS`` (#1608)."""
    plan_spec = stage_hook_plan(stage)
    return ",".join(s for s in (plan_spec.operation_slot, plan_spec.post_slot) if s)


def _discover_local_service_hook_manifests(
    repo_root: Path = REPO_ROOT,
) -> dict[str, str]:
    """Discover local ``runtime-hooks.manifest.json`` files declared by registered IaC services."""
    manifests: dict[str, str] = {}
    for service_id, meta in sorted(service_attrs().items()):
        if not meta.compose_path:
            continue
        rel_dir = Path(meta.compose_path).parent
        rel_manifest = (rel_dir / APP_HOOKS_MANIFEST_PATH).as_posix()
        if (Path(repo_root) / rel_manifest).is_file():
            manifests[service_id] = rel_manifest
    return manifests


LOCAL_SERVICE_HOOK_MANIFESTS: dict[str, str] = _discover_local_service_hook_manifests()


def _local_service_app_url(
    service: str,
    target_env: str,
    domain: str,
) -> str:
    """Resolve the public HTTPS URL for a registered IaC service in ``target_env``."""
    meta = service_attrs().get(service)
    effective_domain = (meta.domain if meta and meta.domain else "") or domain
    subdomain = (meta.subdomain or meta.service) if meta else service.split("/")[-1]
    env_suffix = "" if target_env in ("prod", "production") else "-staging"
    return f"https://{subdomain}{env_suffix}.{effective_domain}"


SOURCE_HASH_FORMULA_PATHS = (
    "common/deploy/deployer.py",
    "common/deploy/config_hash.py",
)


@dataclass(frozen=True)
class ReconcilePlan:
    changed_files: list[str]
    selected: dict[str, str]
    ignored: dict[str, str]
    dropped: list[str]
    staging_services: list[str]
    prod_services: list[str]
    # Selected services deploying to STAGING ONLY because they have no
    # production deployment yet (ServiceSpec.not_yet_in_production, #542 —
    # derived from the Deployer's literal attr). Surfaced so a prod promotion
    # plan shows WHY a service is absent instead of failing on it (v1.1.34).
    not_yet_in_production: list[str] = field(default_factory=list)
    app_release_tags: dict[str, str] = field(default_factory=dict)

    @property
    def services(self) -> list[str]:
        return sorted(set(self.staging_services) | set(self.prod_services))

    def to_dict(self) -> dict:
        return {
            "changed_files": self.changed_files,
            "selected": self.selected,
            "ignored": self.ignored,
            "dropped": self.dropped,
            "services": self.services,
            "staging_services": self.staging_services,
            "prod_services": self.prod_services,
            "not_yet_in_production": self.not_yet_in_production,
            "app_release_tags": dict(self.app_release_tags),
        }


@dataclass(frozen=True)
class DeployCommand:
    service: str
    deploy_type: str
    argv: list[str]

    def to_dict(self) -> dict:
        return {
            "service": self.service,
            "type": self.deploy_type,
            "argv": self.argv,
        }


def is_zero_sha(value: str | None) -> bool:
    return bool(value) and set(value.strip()) == {"0"}


def production_marker_tag(release_tag: str) -> str:
    """Map an immutable release tag to its immutable production marker."""
    if not RELEASE_TAG_RE.fullmatch(release_tag):
        raise ValueError(
            f"production promotion requires a version tag, got {release_tag!r}"
        )
    return f"{PRODUCTION_MARKER_PREFIX}{release_tag}"


def _marker_releases(
    repo_root: Path, runner: Callable[..., subprocess.CompletedProcess]
) -> list[str]:
    """The release of every production marker; a marker that is not ``production/vX.Y.Z`` fails."""
    listed = runner(
        [
            "git",
            "tag",
            "--list",
            f"{PRODUCTION_MARKER_PREFIX}v*.*.*",
            "--sort=-version:refname",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:
        raise SystemExit(
            f"::error::cannot enumerate production markers: {listed.stderr.strip()}"
        )
    markers = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    invalid = [
        marker
        for marker in markers
        if not RELEASE_TAG_RE.fullmatch(marker.removeprefix(PRODUCTION_MARKER_PREFIX))
    ]
    if invalid:
        raise SystemExit(f"::error::invalid production marker(s): {', '.join(invalid)}")
    return [m.removeprefix(PRODUCTION_MARKER_PREFIX) for m in markers]


def assert_production_marker_advances(
    release_tag: str,
    repo_root: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str | None:
    """Validate marker history and return the current production release, if any."""
    production_marker_tag(release_tag)
    candidates = _marker_releases(repo_root, runner)
    if not candidates:
        return None

    current = max(candidates, key=version_key)
    if version_key(release_tag) < version_key(current):
        raise SystemExit(
            f"::error::production promotion cannot move backward from {current} "
            f"to {release_tag}; publish a newer revert release instead"
        )
    return current


def production_before(
    release_tag: str,
    repo_root: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str | None:
    """The release that a production run of ``release_tag`` starts from: the newest marker
    older than ``release_tag``, or None. The one source for the guard, the operation and the
    post phase. The operation writes the marker of ``release_tag``; the post phase after it
    still gets the same answer, because that marker is not older than itself.
    """
    current = assert_production_marker_advances(release_tag, repo_root, runner=runner)
    if current is None or version_key(current) < version_key(release_tag):
        return current
    older = [
        m
        for m in _marker_releases(repo_root, runner)
        if version_key(m) < version_key(release_tag)
    ]
    return max(older, key=version_key, default=None)


def plan_base(
    after: str,
    before: str,
    *,
    promote_prod: bool,
    repo_root: Path,
    previous_tag: bool = False,
) -> str:
    """The ``before`` release of the change set of ``after``.

    Production is cumulative from its own marker (``production_before``): staging-only
    changes of vN+1 and vN+2 are not skipped when vN+3 is promoted. An explicit ``before``
    must equal that marker. Staging starts from ``before``, or from the previous release
    tag when ``previous_tag`` is true.
    """
    if promote_prod:
        marker = production_before(after, repo_root)
        if marker:
            if before and before != marker:
                raise SystemExit(
                    "::error::production before must match current marker release "
                    f"{marker}; got {before}"
                )
            return marker
        if not before:
            raise SystemExit(
                "::error::first production marker requires an explicit known-production "
                "--before release"
            )
        return before
    if before or not previous_tag:
        return before
    listed = subprocess.run(
        [
            "git",
            "tag",
            "--list",
            "v*.*.*",
            "--sort=-v:refname",
            "--merged",
            f"{after}^",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    tags = [t for t in listed.stdout.split() if RELEASE_TAG_RE.fullmatch(t)]
    return tags[0] if listed.returncode == 0 and tags else ""


def releases_between(marker: str, after: str, repo_root: Path) -> list[str]:
    """The release tags newer than ``marker`` up to and including ``after`` (#1563)."""
    listed = subprocess.run(
        ["git", "tag", "--list", "v*.*.*"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(
        (
            t
            for t in listed.stdout.split()
            if RELEASE_TAG_RE.fullmatch(t)
            and version_key(marker) < version_key(t) <= version_key(after)
        ),
        key=version_key,
    )


class ProductionMarkerRejected(Exception):
    """GitHub refused the marker push of the workflow token (#1511)."""


def record_production_marker(
    release_tag: str,
    repo_root: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    """Record a successful explicit production promotion as an immutable tag.

    The marker advances only after all selected production deploys succeed.  A
    retry is idempotent when the existing marker resolves to the same commit;
    moving an existing marker is always refused.
    """
    marker = production_marker_tag(release_tag)
    resolved = runner(
        ["git", "rev-parse", "--verify", f"{release_tag}^{{commit}}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if resolved.returncode != 0:
        raise SystemExit(f"::error::cannot resolve production release {release_tag!r}")
    target_sha = resolved.stdout.strip().lower()

    existing = runner(
        ["git", "rev-parse", "--verify", f"refs/tags/{marker}^{{commit}}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if existing.returncode == 0:
        existing_sha = existing.stdout.strip().lower()
        if existing_sha != target_sha:
            raise SystemExit(
                f"::error::refusing to move immutable production marker {marker}: "
                f"{existing_sha[:12]} != {target_sha[:12]}"
            )
    else:
        tagged = runner(
            ["git", "tag", marker, target_sha],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if tagged.returncode != 0:
            raise SystemExit(
                f"::error::cannot create production marker {marker}: "
                f"{tagged.stderr.strip()}"
            )
    pushed = runner(
        ["git", "push", "origin", f"refs/tags/{marker}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if pushed.returncode != 0:
        detail = pushed.stderr.strip()
        # Measured in #1511: the Actions token cannot create a ref at a commit whose
        # workflow files differ from main. The deploy is done; only the record is late.
        if "refusing to allow a GitHub App" in pushed.stderr:
            raise ProductionMarkerRejected(
                f"cannot push production marker {marker}: {detail}"
            )
        raise SystemExit(f"::error::cannot push production marker {marker}: {detail}")
    return marker


def changed_files_from_git(
    repo_root: Path, before: str | None, after: str
) -> list[str]:
    """Return changed files between two release tags (``before``..``after``).

    ``before`` is the previous release tag; when it is empty/all-zero (the first
    release has no predecessor) diff the tagged commit itself instead of a
    nonexistent range.
    """

    if before and not is_zero_sha(before):
        cmd = ["git", "diff", "--name-only", before, after]
    else:
        cmd = ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", after]
    result = subprocess.run(
        cmd,
        cwd=repo_root,
        capture_output=True,
        check=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def iac_pinned_services() -> list[str]:
    services: list[str] = []
    for service in all_service_keys():
        spec = service_spec(service)
        if spec.iac_pinned:
            services.append(service)
    return sorted(services)


def _parse_repos_yaml_release_tags(raw_text: str) -> dict[str, str]:
    data = yaml.safe_load(raw_text) or {}
    entries = (
        (data.get("repositories") or data.get("repos"))
        if isinstance(data, Mapping)
        else None
    )
    if not isinstance(entries, list):
        return {}
    tags: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("role") != "external-application":
            continue
        repo_id = str(entry.get("id") or "")
        service = APP_REPO_ID_TO_SERVICE.get(repo_id)
        release_tag = entry.get("release_tag")
        if service and isinstance(release_tag, str) and release_tag.strip():
            tags[service] = release_tag.strip()
    return tags


def load_app_release_tags(
    repo_root: Path = REPO_ROOT,
    *,
    rev: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict[str, str]:
    """Read App ``release_tag`` entries from ``harness/repos.yaml`` at ``rev`` or working tree."""
    if rev and not is_zero_sha(rev):
        shown = runner(
            ["git", "show", f"{rev}:{HARNESS_REPOS_PATH}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if shown.returncode != 0:
            return {}
        return _parse_repos_yaml_release_tags(shown.stdout or "")
    target = Path(repo_root) / HARNESS_REPOS_PATH
    if not target.is_file():
        return {}
    return _parse_repos_yaml_release_tags(target.read_text(encoding="utf-8"))


def changed_app_services_from_repos_yaml(
    repo_root: Path = REPO_ROOT,
    *,
    before: str | None = None,
    after: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict[str, str]:
    """Return ``{service: reason}`` for Apps whose ``release_tag`` changed in ``harness/repos.yaml``."""
    after_tags = load_app_release_tags(repo_root, rev=after, runner=runner)
    if not after_tags:
        after_tags = load_app_release_tags(repo_root, rev=None, runner=runner)
    before_tags = (
        load_app_release_tags(repo_root, rev=before, runner=runner)
        if before and not is_zero_sha(before)
        else {}
    )
    selected: dict[str, str] = {}
    for service, after_tag in sorted(after_tags.items()):
        before_tag = before_tags.get(service)
        if before_tag != after_tag:
            detail = f"{before_tag} -> {after_tag}" if before_tag else after_tag
            selected[service] = f"release_tag in {HARNESS_REPOS_PATH} ({detail})"
    if not selected and not before_tags:
        for service, after_tag in sorted(after_tags.items()):
            selected[service] = f"release_tag in {HARNESS_REPOS_PATH} ({after_tag})"
    return selected


def advance_app_release_train(
    service: str,
    *,
    repo_url: str,
    existing_tags: Sequence[str],
    candidate_ref: str = "main",
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict[str, str]:
    """Compute the next ``vX.Y.(Z+1)`` App release tag and resolve ``candidate_ref``."""
    resolved = resolve_image_ref(candidate_ref, repo=repo_url, runner=runner)
    next_tag = next_patch_tag(existing_tags)
    return {
        "service": service,
        "candidate_sha": resolved.sha,
        "image_ref": resolved.image_ref,
        "next_tag": next_app_release_tag(existing_tags, initial=next_tag),
    }


def update_repos_yaml_release_tags(
    repo_root: Path,
    updates: Mapping[str, str],
) -> bool:
    """Update ``release_tag`` entries in ``harness/repos.yaml`` preserving comments."""
    if not updates:
        return False
    target = Path(repo_root) / HARNESS_REPOS_PATH
    text = target.read_text(encoding="utf-8")
    new_text = text
    for service, new_tag in sorted(updates.items()):
        if not RELEASE_TAG_RE.fullmatch(new_tag):
            raise ValueError(f"invalid release tag {new_tag!r} for {service}")
        repo_id = SERVICE_TO_APP_REPO_ID.get(service)
        if not repo_id:
            raise ValueError(f"unknown app service {service!r}")
        pattern = re.compile(
            rf"(^\s*-\s+id:\s+{re.escape(repo_id)}\b[\s\S]*?^\s*release_tag:\s*)(v[0-9]+\.[0-9]+\.[0-9]+)",
            re.MULTILINE,
        )
        new_text, count = pattern.subn(rf"\g<1>{new_tag}", new_text, count=1)
        if count != 1:
            raise ValueError(
                f"could not locate release_tag for {repo_id!r} in {HARNESS_REPOS_PATH}"
            )
    if new_text == text:
        return False
    target.write_text(new_text, encoding="utf-8")
    return True


def _query_remote_app_refs(
    repo_url: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> tuple[str, dict[str, str]]:
    """Return ``(main_sha, {tag_name: commit_sha})`` for ``v*.*.*`` tags in ``repo_url``."""
    listed = runner(
        ["git", "ls-remote", "--refs", "--tags", "--heads", repo_url],
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:
        raise RuntimeError(
            f"git ls-remote failed for {repo_url}: {(listed.stderr or '').strip()}"
        )
    raw_refs: dict[str, str] = {}
    for line in (listed.stdout or "").splitlines():
        parts = line.strip().split()
        if len(parts) == 2:
            raw_refs[parts[1]] = parts[0].strip().lower()
    main_sha = raw_refs.get("refs/heads/main", "")
    peeled = runner(
        ["git", "ls-remote", "--tags", repo_url],
        capture_output=True,
        text=True,
    )
    if peeled.returncode == 0:
        for line in (peeled.stdout or "").splitlines():
            parts = line.strip().split()
            if len(parts) == 2:
                raw_refs[parts[1]] = parts[0].strip().lower()
    tag_shas: dict[str, str] = {}
    for ref, sha in raw_refs.items():
        if not ref.startswith("refs/tags/") or ref.endswith("^{}"):
            continue
        tag = ref.removeprefix("refs/tags/")
        if RELEASE_TAG_RE.fullmatch(tag):
            tag_shas[tag] = raw_refs.get(f"{ref}^{{}}", sha)
    return main_sha, tag_shas


def verify_and_cut_app_preview_tag(
    service: str,
    *,
    repo_slug: str,
    repo_url: str,
    main_sha: str,
    next_tag: str,
    repo_root: Path = REPO_ROOT,
    domain: str = "zitian.party",
    timeout: int = 1800,
    python_executable: str = sys.executable or "python",
    manifest_fetcher: Callable[[str], bytes] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    """Deploy ephemeral preview/branch (main), run preview_gate, cut next_tag, and always tear down (#1554)."""
    assert_app_version_ref_on_main(
        main_sha,
        "staging",
        service=service,
        repo=repo_url,
        repo_root=repo_root,
    )
    preview_cfg = preview_service_config(service)
    effective_domain = domain_for_service(service) or domain
    alias_cfg = preview_alias("branch", "main", slug_prefix=preview_cfg.slug_prefix)
    preview_url = alias_cfg.app_url(
        domain=effective_domain, base_subdomain=preview_cfg.base_subdomain
    )
    try:
        iac_ref = newest_release_tag(repo_root=repo_root, runner=runner)
    except ValueError:
        iac_ref = "main"

    up_argv = [
        python_executable,
        "-m",
        "tools.deploy_v2",
        "--service",
        service,
        "--type",
        "preview/branch",
        "--version-ref",
        "main",
        "--iac-ref",
        iac_ref,
        "--domain",
        effective_domain,
        "--timeout",
        str(timeout),
    ]
    down_argv = [
        python_executable,
        "-m",
        "tools.deploy_v2",
        "--service",
        service,
        "--type",
        "preview/branch",
        "--version-ref",
        "main",
        "--iac-ref",
        iac_ref,
        "--domain",
        effective_domain,
        "--down",
    ]
    try:
        up_res = runner(up_argv, cwd=repo_root, capture_output=True, text=True)
        if up_res.returncode != 0:
            detail = (up_res.stderr or up_res.stdout or "").strip()
            raise RuntimeError(
                f"preview/branch deploy failed for {service}@{main_sha[:7]}: {detail}"
            )
        manifest = fetch_hooks_manifest(
            service,
            main_sha,
            manifest_fetcher=manifest_fetcher,
        )
        hook_cwd, tmp_checkout = resolve_app_hook_cwd(
            service,
            main_sha,
            repo_root=repo_root,
            runner=runner,
        )
        try:
            hook_res = execute_app_hook_slot(
                manifest,
                stage_hook_plan("preview").post_slot,
                app_url=preview_url,
                environment="preview",
                version_ref=main_sha[:7],
                cwd=hook_cwd,
                runner=runner,
            )
        finally:
            if tmp_checkout is not None:
                tmp_checkout.cleanup()
        if hook_res.status != "pass":
            raise RuntimeError(
                f"preview_gate failed for {service}@{main_sha[:7]}: {hook_res.summary}"
            )
        runner(
            [
                "gh",
                "api",
                f"repos/{repo_slug}/git/refs",
                "-f",
                f"ref=refs/tags/{next_tag}",
                "-f",
                f"sha={main_sha}",
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    finally:
        down_res = runner(down_argv, cwd=repo_root, capture_output=True, text=True)
        if down_res.returncode != 0:
            detail = (down_res.stderr or down_res.stdout or "").strip()
            msg = (
                f"preview/branch teardown failed for {service}@{main_sha[:7]}: {detail}"
            )
            if sys.exc_info()[1] is None:
                raise RuntimeError(msg)
            print(f"WARN: {msg}", file=sys.stderr)
    return preview_url


def sync_app_releases(
    repo_root: Path = REPO_ROOT,
    *,
    services: Sequence[str] = (),
    apply_repos_yaml: bool = False,
    sync_gitlinks: bool = False,
    cut_missing_app_tag: bool = False,
    create_pr: bool = False,
    pr_branch: str = "chore/sync-app-releases",
    dry_run: bool = False,
    domain: str = "zitian.party",
    manifest_fetcher: Callable[[str], bytes] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict[str, object]:
    """Pull-based App version sync against remote App repositories (#1416, #1554)."""
    pinned_tags = load_app_release_tags(repo_root, runner=runner)
    target_services = (
        [str(s) for s in services if str(s)] if services else sorted(APP_SOURCES.keys())
    )
    entries: dict[str, dict[str, object]] = {}
    updates: dict[str, str] = {}
    target_shas: dict[str, str] = {}

    for service in target_services:
        repo_slug = APP_SOURCES.get(service)
        if not repo_slug:
            raise ValueError(f"unknown app service {service!r}")
        repo_url = git_url(repo_slug)
        main_sha, tag_shas = _query_remote_app_refs(repo_url, runner=runner)
        sorted_tags = sorted(tag_shas, key=version_key)
        latest_tag = sorted_tags[-1] if sorted_tags else ""
        latest_tag_sha = tag_shas.get(latest_tag, "") if latest_tag else ""
        pinned_tag = pinned_tags.get(service, "")

        main_ahead = bool(
            main_sha and (not latest_tag_sha or main_sha != latest_tag_sha)
        )
        computed_next_tag = (
            next_app_release_tag(sorted_tags) if main_ahead else latest_tag
        )
        target_tag = latest_tag
        target_sha = latest_tag_sha
        preview_verified = False
        preview_url = ""
        if cut_missing_app_tag and main_ahead and computed_next_tag:
            if not dry_run:
                preview_url = verify_and_cut_app_preview_tag(
                    service,
                    repo_slug=repo_slug,
                    repo_url=repo_url,
                    main_sha=main_sha,
                    next_tag=computed_next_tag,
                    repo_root=repo_root,
                    domain=domain,
                    manifest_fetcher=manifest_fetcher,
                    runner=runner,
                )
                preview_verified = True
            target_tag = computed_next_tag
            target_sha = main_sha

        drifted = bool(target_tag and target_tag != pinned_tag)
        if drifted:
            if (apply_repos_yaml or create_pr) and not dry_run:
                assert_app_version_ref_on_main(
                    target_tag,
                    "staging",
                    service=service,
                    repo=repo_url,
                )
            updates[service] = target_tag
            if target_sha:
                target_shas[service] = target_sha

        entries[service] = {
            "service": service,
            "repository": repo_slug,
            "pinned_tag": pinned_tag,
            "latest_remote_tag": latest_tag,
            "latest_remote_tag_sha": latest_tag_sha,
            "main_sha": main_sha,
            "main_ahead_of_latest_tag": main_ahead,
            "next_app_tag": computed_next_tag,
            "target_tag": target_tag,
            "target_sha": target_sha,
            "preview_verified": preview_verified,
            "preview_url": preview_url,
            "needs_update": drifted,
        }

    repos_yaml_updated = False
    gitlinks_updated: list[str] = []
    next_infra_tag = ""
    pr_created = False
    pr_updated = False
    pr_url = ""
    pr_number: int | None = None

    if updates and apply_repos_yaml and not dry_run:
        repos_yaml_updated = update_repos_yaml_release_tags(repo_root, updates)
        if sync_gitlinks:
            for service, sha in sorted(target_shas.items()):
                sub_path = SERVICE_TO_SUBMODULE_PATH.get(service)
                if not sub_path:
                    continue
                updated = runner(
                    [
                        "git",
                        "update-index",
                        "--cacheinfo",
                        f"160000,{sha},{sub_path}",
                    ],
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                )
                if updated.returncode == 0:
                    gitlinks_updated.append(sub_path)

    if updates:
        try:
            current_infra = newest_release_tag(repo_root=repo_root, runner=runner)
            next_infra_tag = next_patch_tag([current_infra])
        except ValueError:
            next_infra_tag = "v0.1.0"
        commit_msg = (
            "chore(train): advance app release tags ("
            + ", ".join(f"{svc}={tag}" for svc, tag in sorted(updates.items()))
            + ")"
        )
        paths_to_add = [HARNESS_REPOS_PATH, *gitlinks_updated]
        if create_pr and repos_yaml_updated and not dry_run:
            orig_head = runner(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            runner(
                ["git", "add", *paths_to_add],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True,
            )
            runner(
                [
                    "git",
                    "-c",
                    "user.name=github-actions[bot]",
                    "-c",
                    "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                    "commit",
                    "-m",
                    commit_msg,
                ],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True,
            )
            runner(
                [
                    "git",
                    "push",
                    "--force-with-lease",
                    "origin",
                    f"HEAD:refs/heads/{pr_branch}",
                ],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True,
            )
            runner(
                ["git", "reset", "--hard", orig_head],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True,
            )
            body_lines = [
                "Automated pull-based App release train sync (#1416).",
                "",
                "### Updated App Release Pins (`harness/repos.yaml`)",
            ]
            for svc, new_tag in sorted(updates.items()):
                old_tag = pinned_tags.get(svc, "<none>")
                sha = target_shas.get(svc, "")
                body_lines.append(
                    f"- `{svc}`: `{old_tag}` -> `{new_tag}` (`{sha[:12]}`)"
                )
            body_lines.append("")
            body_lines.append(
                f"Next `infra2` release tag candidate: `{next_infra_tag}`"
            )
            pr_body = "\n".join(body_lines)

            listed_prs = runner(
                [
                    "gh",
                    "pr",
                    "list",
                    "--head",
                    pr_branch,
                    "--base",
                    "main",
                    "--state",
                    "open",
                    "--json",
                    "number,url",
                ],
                cwd=repo_root,
                capture_output=True,
                text=True,
            )
            existing_prs: list[dict[str, object]] = []
            if listed_prs.returncode == 0 and (listed_prs.stdout or "").strip():
                try:
                    parsed = json.loads(listed_prs.stdout)
                    if isinstance(parsed, list):
                        existing_prs = [p for p in parsed if isinstance(p, dict)]
                except ValueError:
                    existing_prs = []
            if existing_prs:
                raw_num = existing_prs[0].get("number")
                if isinstance(raw_num, int):
                    pr_number = raw_num
                pr_url = str(existing_prs[0].get("url") or "")
                try:
                    runner(
                        [
                            "gh",
                            "pr",
                            "edit",
                            str(pr_number or pr_branch),
                            "--title",
                            commit_msg,
                            "--body",
                            pr_body,
                        ],
                        cwd=repo_root,
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                except subprocess.CalledProcessError as exc:
                    sys.stderr.write(
                        f"gh pr command failed (exit {exc.returncode}): {(exc.stderr or exc.stdout or '').strip()}\n"
                    )
                    raise
                pr_updated = True
            else:
                try:
                    created_pr = runner(
                        [
                            "gh",
                            "pr",
                            "create",
                            "--base",
                            "main",
                            "--head",
                            pr_branch,
                            "--title",
                            commit_msg,
                            "--body",
                            pr_body,
                        ],
                        cwd=repo_root,
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                except subprocess.CalledProcessError as exc:
                    sys.stderr.write(
                        f"gh pr command failed (exit {exc.returncode}): {(exc.stderr or exc.stdout or '').strip()}\n"
                    )
                    raise
                pr_url = (created_pr.stdout or "").strip()
                m_num = re.search(r"/pull/([0-9]+)$", pr_url)
                if m_num:
                    pr_number = int(m_num.group(1))
                pr_created = True

    return {
        "apps": entries,
        "updates": updates,
        "needs_update": bool(updates),
        "repos_yaml_updated": repos_yaml_updated,
        "gitlinks_updated": gitlinks_updated,
        "next_infra_tag": next_infra_tag,
        "pr_branch": pr_branch if create_pr else "",
        "pr_created": pr_created,
        "pr_updated": pr_updated,
        "pr_url": pr_url,
        "pr_number": pr_number,
        "dry_run": dry_run,
    }


#: Deepens the depth-1 hook checkout to the full history of a release tag. The checkout holds
#: the tag's own commit, so a plain tag fetch sends nothing and leaves it shallow. `--unshallow`
#: is not used: git refuses it on a complete repository.
_FULL_HISTORY_DEPTH = "--depth=2147483647"


def _slot_release_ref(
    manifest: RuntimeHooksManifest | Path,
    slots: Sequence[str],
    version_ref: str,
) -> str:
    """Return ``version_ref`` when any requested slot declares ``checkout_lineage=True``."""
    if not isinstance(manifest, RuntimeHooksManifest):
        return ""
    return (
        version_ref
        if any(
            s in manifest.slots and manifest.slots[s].checkout_lineage for s in slots
        )
        else ""
    )


def resolve_app_hook_cwd(
    service: str,
    source_sha: str,
    *,
    release_ref: str = "",
    repo_root: Path = REPO_ROOT,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
    """Return ``(app_cwd, temp_dir_or_none)`` containing an App checkout at ``source_sha`` (#1416).

    A ``vX.Y.Z`` ``release_ref`` is also fetched as a tag with its full commit history and no
    file contents (#1567). A depth-1 fetch of one commit carries no tag names, so an App check
    that runs ``git rev-parse <tag>^{commit}`` fails. Full history is on purpose: a check that
    dates a name with ``git log -S`` on a one-commit history gets the release date and not the
    date that the name first appeared.
    """
    repo_slug = APP_SOURCES.get(service, "")
    if not repo_slug:
        return Path(repo_root), None
    tmp_dir = tempfile.TemporaryDirectory(
        prefix=f"infra2-hook-{service.replace('/', '-')}-"
    )
    checkout_dir = Path(tmp_dir.name)
    try:
        runner(
            ["git", "init", "-q"],
            cwd=checkout_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        runner(
            ["git", "remote", "add", "origin", git_url(repo_slug)],
            cwd=checkout_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        runner(
            ["git", "fetch", "--depth", "1", "origin", source_sha],
            cwd=checkout_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        runner(
            ["git", "checkout", "-q", "FETCH_HEAD"],
            cwd=checkout_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        if RELEASE_TAG_RE.fullmatch(release_ref):
            fetched = runner(
                [
                    "git",
                    "fetch",
                    "-q",
                    "--filter=blob:none",
                    "--no-tags",
                    _FULL_HISTORY_DEPTH,
                    "origin",
                    "tag",
                    release_ref,
                ],
                cwd=checkout_dir,
                capture_output=True,
                text=True,
                check=False,
            )
            if fetched.returncode != 0:
                # The checkout stays valid. The App check reports the missing tag itself.
                sys.stderr.write(
                    f"::warning::release tag {release_ref} was not fetched for {service}: "
                    f"{(fetched.stderr or fetched.stdout or '').strip()}\n"
                )
        return checkout_dir, tmp_dir
    except Exception:
        tmp_dir.cleanup()
        return Path(repo_root), None


def audit_app_hooks(
    repo_root: Path = REPO_ROOT,
    *,
    services: Sequence[str] = (),
    manifest_fetcher: Callable[[str], bytes] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ambient_env: Mapping[str, str] | None = None,
    pinned_tags: Mapping[str, str] | None = None,
    slots: Sequence[str] | None = None,
    target_env: str = "staging",
) -> dict[str, object]:
    """Validate ``runtime-hooks.manifest.json`` contracts for registered Apps (#1416).

    Names mode (``ambient_env`` is None) checks each ``required_env`` name against the
    runner sources. Value mode also builds the env of each slot from ``ambient_env`` and
    fails on a missing or empty value (#1633). ``pinned_tags`` and ``slots`` narrow the check
    to the release train: the pins at the deploy ref, and only the stage slots.
    """
    target_services = (
        [str(s) for s in services if str(s)] if services else sorted(APP_SOURCES.keys())
    )
    if pinned_tags is None:
        pinned_tags = load_app_release_tags(repo_root, runner=runner)
    checked: dict[str, dict[str, object]] = {}
    errors: list[str] = []

    for service in target_services:
        repo_slug = APP_SOURCES.get(service)
        if not repo_slug:
            errors.append(f"unknown app service {service!r}")
            continue
        pinned_tag = pinned_tags.get(service, "")
        manifest: RuntimeHooksManifest | None = None
        source = ""
        try:
            ref = pinned_tag or "main"
            resolved = resolve_image_ref(ref, repo=git_url(repo_slug), runner=runner)
            manifest = fetch_hooks_manifest(
                service,
                resolved.sha.lower(),
                manifest_fetcher=manifest_fetcher,
            )
            source = f"{repo_slug}@{resolved.sha[:12]}:{APP_HOOKS_MANIFEST_PATH}"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{service}: {exc}")
            checked[service] = {"ok": False, "error": str(exc)}
            continue

        supplied = set(HOOK_RUNNER_SECRET_NAMES) | set(HOOK_RUNNER_CONTEXT_ENV)
        slot_errors: list[str] = []
        if manifest.schema_version != 2:
            slot_errors.append(
                f"expected schema_version 2, got {manifest.schema_version!r}"
            )
        for slot_name in slots or HOOK_SLOTS:
            if slot_name not in manifest.slots:
                slot_errors.append(f"missing slot {slot_name!r}")
                continue
            spec = manifest.slots[slot_name]
            for env_name in (*spec.required_env, *spec.optional_env):
                if env_name in FORBIDDEN_HOOK_ENV_NAMES or env_name.startswith(
                    FORBIDDEN_HOOK_ENV_PREFIXES
                ):
                    slot_errors.append(
                        f"forbidden secret {env_name!r} in slot {slot_name!r}"
                    )
            for env_name in spec.required_env:
                if env_name not in supplied:
                    slot_errors.append(
                        f"unsupplied required_env {env_name!r} in slot {slot_name!r}"
                    )
            if ambient_env is None:
                continue
            try:
                hook_env_for_slot(
                    manifest,
                    slot_name,
                    app_url=_app_url_for(service, target_env, "zitian.party"),
                    environment="production"
                    if target_env in ("prod", "production")
                    else "staging",
                    version_ref=pinned_tag or "main",
                    ambient_env=ambient_env,
                )
            except ValueError as exc:
                slot_errors.append(f"{slot_name}: {exc}")
        if slot_errors:
            errors.extend(f"{service}: {err}" for err in slot_errors)
            checked[service] = {
                "ok": False,
                "source": source,
                "pinned_tag": pinned_tag,
                "errors": slot_errors,
            }
        else:
            checked[service] = {
                "ok": True,
                "source": source,
                "pinned_tag": pinned_tag,
                "slots": list(manifest.slots.keys()),
            }

    return {
        "ok": len(errors) == 0,
        "services": checked,
        "errors": errors,
    }


def _missing_env_errors(audit: Mapping[str, object]) -> dict[str, str]:
    """Return ``{service: message}`` for each audited service with a missing hook env value."""
    failures: dict[str, str] = {}
    for service, info in dict(audit.get("services", {})).items():  # type: ignore[call-overload]
        lines = [e for e in info.get("errors", ()) if "missing required hook env:" in e]
        if lines:
            failures[service] = f"{service}: " + "; ".join(lines)
    return failures


def _app_url_for(service: str, target_env: str, domain: str) -> str:
    """Return the public URL of an App service in ``target_env``."""
    norm_env = "prod" if target_env in ("prod", "production") else "staging"
    return app_compose_env_config(service, norm_env).app_url(
        domain=domain_for_service(service) or domain
    )


def resolve_app_hook_coordinates(
    service: str,
    *,
    repo_root: Path = REPO_ROOT,
    after: str = "",
    target_env: str = "staging",
    domain: str = "zitian.party",
    source_sha: str = "",
    app_url: str = "",
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> tuple[str, str, str]:
    """Resolve ``(version_ref, source_sha, app_url)`` for an App hook invocation (#1416)."""
    repo_slug = APP_SOURCES.get(service)
    if not repo_slug:
        raise ValueError(f"unknown app service {service!r}")
    tags = load_app_release_tags(
        repo_root, rev=after or None, runner=runner
    ) or load_app_release_tags(repo_root, rev=None, runner=runner)
    version_ref = tags.get(service) or after or "main"
    resolved_sha = source_sha.strip()
    if not resolved_sha:
        resolved = resolve_image_ref(
            version_ref, repo=git_url(repo_slug), runner=runner
        )
        resolved_sha = resolved.sha.lower()
    return (
        version_ref,
        resolved_sha,
        app_url.strip() or _app_url_for(service, target_env, domain),
    )


def _parse_hook_slots(slot_spec: str) -> tuple[str, ...]:
    """Parse and validate a comma-separated hook slot specification (#1493)."""
    raw_items = [part.strip() for part in str(slot_spec).split(",") if part.strip()]
    if not raw_items:
        raise ValueError("hook slot specification must name at least one slot")
    seen: list[str] = []
    for item in raw_items:
        if item not in HOOK_SLOTS:
            raise ValueError(f"unknown hook slot {item!r}; allowed: {list(HOOK_SLOTS)}")
        if item not in seen:
            seen.append(item)
    return tuple(seen)


def _hook_env_failure(
    service: str,
    slot: str,
    environment: str,
    app_url: str,
    version_ref: str,
    message: str,
) -> HookRunResult:
    """Return the failed result of a hook slot that never ran because of a missing env name."""
    return HookRunResult(
        service=service,
        slot=slot,
        environment=environment,
        app_url=app_url,
        version_ref=version_ref,
        status="fail",
        exit_code=1,
        duration_seconds=0.0,
        summary=message,
        stderr_tail=message,
    )


def _run_hook_slot_or_fail(
    manifest: RuntimeHooksManifest,
    slot: str,
    *,
    app_url: str,
    environment: str,
    version_ref: str,
    **kwargs: object,
) -> tuple[HookRunResult, str | None]:
    """Run one hook slot. A missing env name gives a failed result and its message, not a raise."""
    try:
        res = execute_app_hook_slot(
            manifest,
            slot,
            app_url=app_url,
            environment=environment,
            version_ref=version_ref,
            **kwargs,  # type: ignore[arg-type]
        )
    except ValueError as exc:
        if "missing required hook env:" not in str(exc):
            raise
        message = str(exc)
        return (
            _hook_env_failure(
                manifest.service, slot, environment, app_url, version_ref, message
            ),
            message,
        )
    return res, None


def run_post_deploy_app_hooks(
    plan: ReconcilePlan,
    applied: Sequence[DeployCommand],
    *,
    repo_root: Path = REPO_ROOT,
    after: str = "",
    target_env: str = "staging",
    domain: str = "zitian.party",
    slot: str = "smoke",
    manifest_fetcher: Callable[[str], bytes] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[dict[str, object]]:
    """Execute post-deploy verification hook slot(s) for each deployed App or local service (#1416, #1493, #1521)."""
    slots = _parse_hook_slots(slot)
    local_manifests = _discover_local_service_hook_manifests(repo_root)
    applied_services = {
        part.strip()
        for cmd in applied
        for part in cmd.service.split(",")
        if part.strip() in APP_SOURCES
        or part.strip() in local_manifests
        or part.strip() in LOCAL_SERVICE_HOOK_MANIFESTS
    }

    def _run_for_service(service: str) -> list[dict[str, object]]:
        rel_local = local_manifests.get(service) or LOCAL_SERVICE_HOOK_MANIFESTS.get(
            service
        )
        local_path = (Path(repo_root) / rel_local) if rel_local else None
        if service not in APP_SOURCES and local_path is not None:
            if not local_path.is_file():
                local_path = REPO_ROOT / rel_local  # type: ignore[arg-type]
            manifest = load_hooks_manifest(local_path)
            version_ref = after or "main"
            source_sha = after or "local"
            app_url = _local_service_app_url(service, target_env, domain)
            hook_cwd = local_path.parent
            tmp_checkout = None
        else:
            version_ref = plan.app_release_tags.get(service) or after
            _, source_sha, app_url = resolve_app_hook_coordinates(
                service,
                repo_root=repo_root,
                after=after,
                target_env=target_env,
                domain=domain,
                runner=runner,
            )
            manifest = fetch_hooks_manifest(
                service,
                source_sha,
                manifest_fetcher=manifest_fetcher,
            )
            hook_cwd, tmp_checkout = resolve_app_hook_cwd(
                service,
                source_sha,
                release_ref=_slot_release_ref(manifest, slots, version_ref),
                repo_root=repo_root,
                runner=runner,
            )
        svc_results: list[dict[str, object]] = []
        try:
            for single_slot in slots:
                hook_env_name = (
                    "production" if target_env in ("prod", "production") else "staging"
                )
                res, missing_env_error = _run_hook_slot_or_fail(
                    manifest,
                    single_slot,
                    app_url=app_url,
                    environment=hook_env_name,
                    version_ref=version_ref,
                    cwd=hook_cwd,
                    runner=runner,
                )
                entry = res.to_dict()
                entry["service"] = service
                entry["service_id"] = service
                entry["slot"] = single_slot
                entry["ok"] = res.status == "pass"
                entry["app_url"] = app_url
                entry["source_sha"] = source_sha
                entry["cwd"] = str(hook_cwd)
                if missing_env_error is not None:
                    entry["missing_env_error"] = missing_env_error
                svc_results.append(entry)
                if res.status != "pass":
                    break
        finally:
            if tmp_checkout is not None:
                tmp_checkout.cleanup()
        return svc_results

    services_sorted = sorted(applied_services)
    hook_results: list[dict[str, object]] = []
    if len(services_sorted) <= 1:
        for svc in services_sorted:
            hook_results.extend(_run_for_service(svc))
    else:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(services_sorted)
        ) as pool:
            futures = {
                svc: pool.submit(_run_for_service, svc) for svc in services_sorted
            }
            for svc in services_sorted:
                hook_results.extend(futures[svc].result())
    return hook_results


def run_scheduled_app_audits(
    repo_root: Path = REPO_ROOT,
    *,
    target_env: str = "staging",
    slot: str = "scheduled_audit",
    domain: str = "zitian.party",
    base_domain: str = "",
    env_overrides: Mapping[str, str] | None = None,
    manifest_fetcher: Callable[[str], bytes] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    services: Sequence[str] | None = None,
) -> list[dict[str, object]]:
    """Execute a scheduled runtime hook slot across registered Apps and local services (#1493, #1521)."""
    if slot not in HOOK_SLOTS:
        raise ValueError(f"unknown hook slot {slot!r}; allowed: {list(HOOK_SLOTS)}")
    effective_domain = base_domain.strip() or domain
    local_manifests = _discover_local_service_hook_manifests(repo_root)
    default_services = sorted(set(APP_SOURCES.keys()) | set(local_manifests.keys()))
    target_services = (
        [str(s) for s in services if str(s)] if services else default_services
    )
    pinned_tags = load_app_release_tags(repo_root, runner=runner)
    effective_ambient: dict[str, str] = dict(os.environ)
    if env_overrides:
        for k, v in env_overrides.items():
            if v is not None:
                effective_ambient[str(k)] = str(v)

    app_targets = [svc for svc in target_services if svc in APP_SOURCES]
    env_failures = (
        _missing_env_errors(
            audit_app_hooks(
                repo_root,
                services=app_targets,
                manifest_fetcher=manifest_fetcher,
                runner=runner,
                ambient_env=effective_ambient,
                pinned_tags=pinned_tags,
                slots=(slot,),
                target_env=target_env,
            )
        )
        if app_targets
        else {}
    )
    hook_env_name = "production" if target_env in ("prod", "production") else "staging"

    results: list[dict[str, object]] = []
    for service in target_services:
        if service in env_failures:
            message = env_failures[service]
            version_ref = pinned_tags.get(service) or "main"
            app_url = _app_url_for(service, target_env, effective_domain)
            entry = _hook_env_failure(
                service, slot, hook_env_name, app_url, version_ref, message
            ).to_dict()
            entry.update(
                service=service,
                service_id=service,
                slot=slot,
                ok=False,
                app_url=app_url,
                missing_env_error=message,
            )
            results.append(entry)
            continue
        rel_local = local_manifests.get(service) or LOCAL_SERVICE_HOOK_MANIFESTS.get(
            service
        )
        if service not in APP_SOURCES and rel_local:
            local_path = Path(repo_root) / rel_local
            if not local_path.is_file():
                local_path = REPO_ROOT / rel_local
            manifest = load_hooks_manifest(local_path)
            if slot not in manifest.slots:
                continue
            version_ref = "main"
            source_sha = "local"
            app_url = _local_service_app_url(service, target_env, effective_domain)
            hook_cwd = local_path.parent
            tmp_checkout = None
        else:
            repo_slug = APP_SOURCES.get(service)
            if not repo_slug:
                raise ValueError(f"unknown app service {service!r}")
            version_ref, source_sha, app_url = resolve_app_hook_coordinates(
                service,
                repo_root=repo_root,
                after=pinned_tags.get(service, ""),
                target_env=target_env,
                domain=effective_domain,
                runner=runner,
            )
            manifest = fetch_hooks_manifest(
                service,
                source_sha,
                manifest_fetcher=manifest_fetcher,
            )
            if slot not in manifest.slots:
                continue
            hook_cwd, tmp_checkout = resolve_app_hook_cwd(
                service,
                source_sha,
                release_ref=_slot_release_ref(manifest, (slot,), version_ref),
                repo_root=repo_root,
                runner=runner,
            )
        try:
            res, missing_env_error = _run_hook_slot_or_fail(
                manifest,
                slot,
                app_url=app_url,
                environment=hook_env_name,
                version_ref=version_ref,
                cwd=hook_cwd,
                ambient_env=effective_ambient,
                runner=runner,
            )
        finally:
            if tmp_checkout is not None:
                tmp_checkout.cleanup()
        entry = res.to_dict()
        entry["service"] = service
        entry["service_id"] = service
        entry["slot"] = slot
        entry["ok"] = res.status == "pass"
        entry["app_url"] = app_url
        entry["source_sha"] = source_sha
        entry["cwd"] = str(hook_cwd)
        if missing_env_error is not None:
            entry["missing_env_error"] = missing_env_error
        results.append(entry)

    return results


def build_plan(
    changed_files: Sequence[str],
    *,
    repo_root: Path = REPO_ROOT,
    before: str | None = None,
    after: str | None = None,
    app_release_tags: Mapping[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> ReconcilePlan:
    files = sorted(dict.fromkeys(str(path) for path in changed_files if str(path)))
    decision = explain_fanout(files)
    selected: dict[str, str] = {}
    ignored: dict[str, str] = {}
    dropped = list(decision.dropped)

    effective_app_tags = (
        dict(app_release_tags)
        if app_release_tags is not None
        else (
            load_app_release_tags(repo_root, rev=after, runner=runner)
            or load_app_release_tags(repo_root, rev=None, runner=runner)
        )
    )

    if MANIFEST_PATH in files:
        for service in iac_pinned_services():
            selected.setdefault(
                service, f"dependency manifest changed ({MANIFEST_PATH})"
            )

    formula_hits = [p for p in SOURCE_HASH_FORMULA_PATHS if p in files]
    if formula_hits:
        for service in iac_pinned_services():
            selected.setdefault(
                service, f"source config hash formula changed ({formula_hits[0]})"
            )

    if HARNESS_REPOS_PATH in files:
        repos_selected = changed_app_services_from_repos_yaml(
            repo_root, before=before, after=after, runner=runner
        )
        if not repos_selected and effective_app_tags:
            repos_selected = {
                svc: f"release_tag in {HARNESS_REPOS_PATH} ({tag})"
                for svc, tag in sorted(effective_app_tags.items())
            }
        for svc, reason in sorted(repos_selected.items()):
            selected[svc] = reason
        if repos_selected and HARNESS_REPOS_PATH in dropped:
            dropped.remove(HARNESS_REPOS_PATH)

    for service, reason in sorted(decision.selected.items()):
        try:
            spec = service_spec(service)
        except ValueError:
            ignored[service] = "not registered in deploy_v2 service registry"
            continue
        if not spec.iac_pinned:
            app_tag = effective_app_tags.get(service, "")
            if app_tag:
                selected.setdefault(service, f"{reason}; release_tag={app_tag}")
            else:
                ignored[service] = (
                    "not iac_pinned; fixed app deploys need an explicit version_ref"
                )
            continue
        selected[service] = reason

    staging_services: list[str] = []
    prod_services: list[str] = []
    not_yet_in_production: list[str] = []
    for service in sorted(selected):
        spec = service_spec(service)
        # #542: a service with no production deployment yet (staging-scoped
        # rollout, e.g. truealpha #500) deploys to staging only — including it
        # in the prod fan-out is exactly what failed the v1.1.34 promote 2/14
        # (no prod composes / Vault provisioning to deploy to).
        if spec.not_yet_in_production:
            staging_services.append(service)
            not_yet_in_production.append(service)
        elif spec.prod_only:
            prod_services.append(service)
        else:
            staging_services.append(service)
            prod_services.append(service)

    return ReconcilePlan(
        changed_files=files,
        selected=dict(sorted(selected.items())),
        ignored=dict(sorted(ignored.items())),
        dropped=sorted(dropped),
        staging_services=staging_services,
        prod_services=prod_services,
        not_yet_in_production=not_yet_in_production,
        app_release_tags=dict(sorted(effective_app_tags.items())),
    )


def _single_service_deploy_command(
    service: str,
    deploy_type: str,
    *,
    version_ref: str,
    iac_ref: str,
    domain: str,
    timeout: int,
    staging_validated: bool,
    python_executable: str,
) -> DeployCommand:
    argv = [
        python_executable,
        "-m",
        "tools.deploy_v2",
        "--service",
        service,
        "--type",
        deploy_type,
        "--version-ref",
        version_ref,
        "--iac-ref",
        iac_ref,
        "--domain",
        domain,
        "--timeout",
        str(timeout),
    ]
    if deploy_type == "prod":
        argv.append("--code-reviewed")
        if staging_validated:
            argv.append("--staging-validated")
    return DeployCommand(service=service, deploy_type=deploy_type, argv=argv)


def build_deploy_commands(
    plan: ReconcilePlan,
    *,
    iac_ref: str,
    domain: str,
    timeout: int,
    python_executable: str = sys.executable,
) -> list[DeployCommand]:
    commands: list[DeployCommand] = []
    for deploy_type, services in (
        ("staging", plan.staging_services),
        ("prod", plan.prod_services),
    ):
        if not services:
            continue
        app_services = [svc for svc in services if not service_spec(svc).iac_pinned]
        companion_services = {
            comp for svc in app_services for comp in service_spec(svc).companions
        }
        platform_services = [
            svc
            for svc in services
            if service_spec(svc).iac_pinned and svc not in companion_services
        ]
        if platform_services:
            argv = [
                python_executable,
                "-m",
                "tools.deploy_v2",
                "--service",
                ",".join(platform_services),
                "--type",
                deploy_type,
                # iac_pinned services ignore version_ref (their artifact IS the iac_ref stack),
                # but staging/prod accept tags only — pin both axes to the promoted release tag
                # so the command is self-consistent and never carries a moving ref to a fixed env.
                "--version-ref",
                iac_ref,
                "--iac-ref",
                iac_ref,
                "--domain",
                domain,
                "--timeout",
                str(timeout),
            ]
            if deploy_type == "prod":
                argv.append("--code-reviewed")
                if set(platform_services) & set(plan.staging_services):
                    argv.append("--staging-validated")
            commands.append(
                DeployCommand(
                    service=",".join(platform_services),
                    deploy_type=deploy_type,
                    argv=argv,
                )
            )
        for app_svc in app_services:
            app_tag = plan.app_release_tags.get(app_svc)
            if not app_tag:
                raise ValueError(
                    f"missing release_tag in {HARNESS_REPOS_PATH} for {app_svc!r}"
                )
            staging_ok = app_svc in plan.staging_services
            commands.append(
                _single_service_deploy_command(
                    app_svc,
                    deploy_type,
                    version_ref=app_tag,
                    iac_ref=iac_ref,
                    domain=domain,
                    timeout=timeout,
                    staging_validated=staging_ok,
                    python_executable=python_executable,
                )
            )
            for companion in service_spec(app_svc).companions:
                spec = service_spec(companion)
                if deploy_type == "prod" and spec.not_yet_in_production:
                    continue
                if deploy_type == "staging" and spec.prod_only:
                    continue
                commands.append(
                    _single_service_deploy_command(
                        companion,
                        deploy_type,
                        version_ref=app_tag,
                        iac_ref=iac_ref,
                        domain=domain,
                        timeout=timeout,
                        staging_validated=staging_ok,
                        python_executable=python_executable,
                    )
                )
    return commands


def commands_to_apply(
    commands: Sequence[DeployCommand],
    *,
    dry_run: bool,
    promote_prod: bool = False,
    target_env: str | None = None,
) -> list[DeployCommand]:
    """Pick which deploy commands actually run, per the release-decoupling gate (#940).

    A release tag push **auto-applies staging** (soak); **prod is an explicit,
    separately-triggered promotion** (``--env prod`` / ``--promote-prod``), never automatic — so
    'cutting a tag' stops meaning 'touch prod'. ``--dry-run`` applies nothing
    (plan only). The complement (commands NOT selected) is surfaced as a plan so
    operators see the deferred prod promotion.
    """
    if dry_run:
        return []
    is_prod = promote_prod or (target_env in ("prod", "production"))
    wanted = "prod" if is_prod else "staging"
    return [command for command in commands if command.deploy_type == wanted]


# Owner decision (2026-11-05, #1439): the approval of a production tag covers at most two
# automatic retries of a failed unit, in the same run and on the same tag (same command).
MAX_UNIT_RETRIES = 2
UNIT_RETRY_BACKOFF_SECONDS = (30.0, 90.0)


def _is_platform_unit(command: DeployCommand) -> bool:
    """True for a unit of iac_pinned services (the runner path). An app unit goes through
    promote, whose failure rolls production back; it is never retried."""
    names = [name for name in command.service.split(",") if name]
    try:
        return bool(names) and all(service_spec(name).iac_pinned for name in names)
    except Exception:  # noqa: BLE001 - an unknown service is not retried
        return False


def run_deploy_commands(
    commands: Sequence[DeployCommand],
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], str] = lambda: datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    ),
) -> list[dict]:
    """Run each deploy command; retry a failed platform unit on a transient fault.

    At most MAX_UNIT_RETRIES retries, only when ``classify_failure`` calls the failure
    transient. Every attempt is recorded as ``attempts: [{n, cause, at}]`` (``cause`` is
    why it failed, None when it succeeded). Idempotency: the retry is the same signed
    ``/deploy`` request (same ``deployment_id``), and the config-hash gate skips a service
    that already matches. An app unit is never retried: its failure triggers promote's
    rollback, which a re-run repeats until the circuit breaker trips.
    """
    results: list[dict] = []
    for command in commands:
        attempts: list[dict] = []
        began = time.monotonic()
        while True:
            attempt = {"n": len(attempts) + 1, "cause": None, "at": clock()}
            attempts.append(attempt)
            completed = runner(command.argv, capture_output=True, text=True)
            if completed.returncode == 0:
                break
            transient, attempt["cause"] = classify_failure(completed.stderr)
            if not (transient and _is_platform_unit(command)):
                break
            if attempt["n"] > MAX_UNIT_RETRIES:
                break
            pause = UNIT_RETRY_BACKOFF_SECONDS[attempt["n"] - 1]
            print(
                f"reconcile retry: {command.deploy_type} {command.service}: attempt "
                f"{attempt['n'] + 1} of {MAX_UNIT_RETRIES + 1} after {attempt['cause']} "
                f"(same command, same tag; waiting {pause:g}s)",
                file=sys.stderr,
                flush=True,
            )
            sleep(pause)
        record = {
            "service": command.service,
            "type": command.deploy_type,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "attempts": attempts,
            "seconds": round(time.monotonic() - began, 3),
        }
        if completed.returncode == 0 and completed.stdout.strip():
            try:
                record["deploy_v2"] = json.loads(completed.stdout)
            except json.JSONDecodeError:
                record["deploy_v2_parse_error"] = "stdout was not JSON"
        results.append(record)
        if completed.returncode != 0:
            break
    return results


_RUNNER_FINAL_RE = re.compile(
    r"ended '(?P<status>[a-z_]+)': (?P<payload>\{.*\})\s*$", re.S
)


def failure_summary_from_result(result: Mapping[str, object]) -> list[dict]:
    """The iac-runner's per-service diagnostics behind a failed deploy_v2 command.

    deploy_v2 raises with the runner's final status record appended as a Python literal
    (``... ended 'failed': {...}``); that record carries ``failure_summary`` — one entry per
    failed service with the runner's ``error_kind`` / ``summary`` / ``next_action`` — either
    at the top level or under ``details``. Anything unparseable yields an empty list so the
    caller falls back to the stderr tail; the alert is never silent because parsing failed.
    """
    stderr = str(result.get("stderr") or "")
    match = _RUNNER_FINAL_RE.search(stderr)
    if not match:
        return []
    try:
        final = ast.literal_eval(match.group("payload"))
    except (ValueError, SyntaxError):
        return []
    if not isinstance(final, dict):
        return []
    for candidate in (final, final.get("details")):
        if isinstance(candidate, dict) and isinstance(
            candidate.get("failure_summary"), list
        ):
            return [
                entry
                for entry in candidate["failure_summary"]
                if isinstance(entry, dict)
            ]
    return []


def github_run_url(env: Mapping[str, str]) -> str:
    """The Actions run URL, or a clear placeholder when any part is missing (a local
    run must not produce the misleading "actions/runs")."""
    parts = (
        env.get("GITHUB_SERVER_URL"),
        env.get("GITHUB_REPOSITORY"),
        env.get("GITHUB_RUN_ID"),
    )
    if not all(parts):
        return "(run URL unavailable outside GitHub Actions)"
    server, repository, run_id = parts
    return f"{server}/{repository}/actions/runs/{run_id}"


#: The state of a failure alert, from the train job that failed (#1646).
ALERT_STATE_BY_JOB = {
    "pre": "refused",
    "operation": "deploy failed",
    "post": "hook failed",
}


def failure_alert_text(
    *,
    after: str,
    results: Sequence[Mapping[str, object]],
    run_url: str,
    promote_prod: bool,
    hook_results: Sequence[Mapping[str, object]] = (),
    failed_job: str = "",
    failed_checks: Sequence[CheckVerdict] = (),
) -> str | None:
    """One Feishu message for a failed reconcile: which tag, which environment, and per
    service the runner's error_kind and next action — the earliest machine-readable point
    of every deploy-chain incident of 2026-09-04..08 (infra2#658), or an explicit
    post-deploy hook alert when container deploys succeeded (#1577). ``None`` when nothing
    failed.

    ``failed_job`` is the train job that failed (``pre``, ``operation``, ``post``). It sets
    the state in ``ALERT_STATE_BY_JOB``: ``pre`` is "refused" and lists the ``failed_checks``;
    ``operation`` is "deploy failed" (a failed ``smoke`` hook is "hook failed"); ``post`` is
    "hook failed". Without a job the text is the one that a single-job run sent.
    """
    kind = "PRODUCTION promotion" if promote_prod else "staging soak"
    if failed_job == "pre":
        lines = [
            f"🛑 IaC reconcile REFUSED for {after} ({kind}): {ALERT_STATE_BY_JOB['pre']}; "
            "nothing was deployed."
        ]
        lines += [
            f"- {check.name}: {check.detail.strip()[:200] or 'failed'}"
            for check in failed_checks
            if not check.ok
        ]
        lines.append(f"Run: {run_url}")
        return "\n".join(lines)
    failed = [item for item in results if item.get("returncode") != 0]
    state = f": {ALERT_STATE_BY_JOB['operation']}." if failed_job and failed else ""
    if failed:
        lines = [f"🚨 IaC reconcile FAILED for {after} ({kind}){state or '.'}"]
        for item in failed:
            env = str(item.get("type") or "?")
            services = str(item.get("service") or "?")
            summary = failure_summary_from_result(item)
            if summary:
                for entry in summary:
                    lines.append(
                        f"- {env} {entry.get('service') or services}: {entry.get('error_kind') or 'unknown'}"
                        f" — {str(entry.get('next_action') or entry.get('summary') or '').strip()[:200]}"
                    )
            else:
                stderr_lines = [
                    line.strip()
                    for line in str(item.get("stderr") or "").strip().splitlines()
                    if line.strip()
                ]
                # deploy_v2's own verdict line names the failure; the lines after it are
                # library boilerplate ("For more information check: https://developer...")
                # — the first real page (v1.1.73, 2026-09-08 11:01Z) quoted the boilerplate.
                verdict = [
                    line
                    for line in stderr_lines
                    if line.startswith("deploy_v2 failed:")
                ]
                detail = (verdict or stderr_lines or ["no diagnostic"])[-1]
                lines.append(f"- {env} {services}: {detail[:200]}")
        lines.append(f"Run: {run_url}")
        return "\n".join(lines)

    failed_hooks = [item for item in hook_results if item.get("status") != "pass"]
    if failed_hooks:
        head = f"⚠️ Post-deploy hook FAILED for {after} ({kind}; container deploys succeeded)."
        lines = [head[:-1] + f": {ALERT_STATE_BY_JOB['post']}." if failed_job else head]
        for item in failed_hooks:
            svc = str(item.get("service") or "?")
            slot = str(item.get("slot") or "?")
            vref = str(item.get("version_ref") or after or "?")
            raw_summary = (
                str(item.get("summary") or "").strip()
                or str(item.get("stderr_tail") or "").strip()
                or "hook failed"
            )
            summary_line = raw_summary.splitlines()[0][:200]
            lines.append(f"- {svc} [{slot} @ {vref}]: {summary_line}")
        lines.append(f"Run: {run_url}")
        return "\n".join(lines)

    # a phase that stopped on a check before any deploy result (``hook_env`` of the operation)
    if failed_job and any(not check.ok for check in failed_checks):
        lines = [
            f"🚨 IaC reconcile FAILED for {after} ({kind}): {ALERT_STATE_BY_JOB[failed_job]}."
        ]
        lines += [
            f"- {check.name}: {check.detail.strip()[:200] or 'failed'}"
            for check in failed_checks
            if not check.ok
        ]
        lines.append(f"Run: {run_url}")
        return "\n".join(lines)

    return None


def resolve_workflow_failure_alert(
    result_path: Path,
    *,
    after: str,
    promote_prod: bool,
    run_url: str,
    reconcile_phase: str = "",
    failed_job: str = "",
) -> tuple[str | None, str | None]:
    """Resolve ``(alert_text_to_send, skip_notice)`` for the workflow ``Alert on failure`` step (#1577).

    The failed phase writes its own ``alert_text`` into the result file. When it wrote none
    (it crashed first) the fallback names the state of ``failed_job``.
    """
    if reconcile_phase == "sync-app-releases" and not result_path.exists():
        return (
            None,
            "::warning::App release PR sync failed before reconcile-deploy phase; skipping deploy-outage Feishu alert.",
        )
    if result_path.exists():
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, Mapping):
            alert_text = data.get("alert_text")
            if isinstance(alert_text, str) and alert_text.strip():
                return (alert_text, None)
    kind = "PRODUCTION promotion" if promote_prod else "staging soak"
    state = ALERT_STATE_BY_JOB.get(failed_job, "")
    crash_fallback_text = (
        f"🚨 IaC reconcile FAILED for {after} ({kind}) before any "
        f"deploy result was recorded (guard, provenance or runner unreachable)"
        + (f"; state: {state}" if state else "")
        + f".\nRun: {run_url}"
    )
    return (crash_fallback_text, None)


def hook_findings_from_result(
    result_path: Path,
) -> tuple[list[tuple[str, str]], list[str]]:
    """The App hooks of one run: ``(red (key, detail) pairs, keys that ran green)`` (#1634).

    The keys are ``app_hook_pair`` keys, the same ones the nightly audit pages. An
    unreadable or missing result file gives two empty lists: no hook was evaluated.
    """
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], []
    items = data.get("hook_results") if isinstance(data, Mapping) else None
    red: list[tuple[str, str]] = []
    green: list[str] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, Mapping):
            continue
        pair = app_hook_pair(item)
        if item.get("status") == "pass":
            green.append(pair[0])
        else:
            red.append(pair)
    return sorted(red), green


_HARNESS_DIRS = frozenset({"tools", "common", "harness", ".github"})


def plan_units(plan: ReconcilePlan) -> tuple[str, ...]:
    """The units of a stage record: services of the plan, changed harness trees and the SDK pin."""
    harness = {f.split("/", 1)[0] for f in plan.dropped if "/" in f} & _HARNESS_DIRS
    return (
        *(f"{'app' if s in APP_SOURCES else 'infra'}:{s}" for s in plan.services),
        *(f"harness:{d}" for d in sorted(harness)),
        sdk_unit(),
    )


@contextmanager
def timed_check(checks: list[CheckVerdict], name: str):
    """Append one check for the block: green when it ends, red (and re-raise) when it raises."""
    began = time.monotonic()
    try:
        yield
    except BaseException as exc:
        detail = str(exc) or type(exc).__name__
        checks.append(
            CheckVerdict(
                name, False, detail[:300], seconds=round(time.monotonic() - began, 3)
            )
        )
        raise
    checks.append(
        CheckVerdict(name, True, "", seconds=round(time.monotonic() - began, 3))
    )


class StageRun:
    """The records that one process writes. ``emit`` writes one record; the ``alert`` job of the
    workflow renders the card from the records of all three jobs (#1646)."""

    def __init__(
        self, *, env: str, subject: str, dry_run: bool, units: Sequence[str] = ()
    ) -> None:
        self.env, self.subject, self.dry_run, self.units = (
            env,
            subject,
            dry_run,
            tuple(units),
        )
        github_event = os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch")
        self.event = {"push": "tag", "schedule": "schedule"}.get(
            github_event, "dispatch"
        )
        self.ref = os.environ.get("GITHUB_REF") or subject
        self.run_url = github_run_url(os.environ)
        self.records: list[StageRecord] = []

    def emit(
        self,
        phase: str,
        started_at: str,
        checks: Sequence[CheckVerdict],
        uncovered: Sequence[str] = (),
    ) -> None:
        """A record that cannot be written is a warning: a record must not stop a deploy."""
        try:
            record = write_stage_record(
                env=self.env,
                phase=phase,
                subject=self.subject,
                ref=self.ref,
                event=self.event,
                run_url=self.run_url,
                started_at=started_at,
                checks=checks,
                units=self.units,
                uncovered=uncovered,
                persist=not self.dry_run,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"::warning::stage record {phase} not written: {type(exc).__name__}: {exc}"
            )
            return
        self.records.append(record)


def operation_checks(results: Sequence[dict], *, dry_run: bool) -> list[CheckVerdict]:
    """One check for each deploy command that ran. A run without a command has one green check."""
    if not results:
        return [
            CheckVerdict(
                "deploy", True, "dry run" if dry_run else "no deploy command applied"
            )
        ]
    checks = []
    for item in results:
        attempts = item.get("attempts") or []
        detail = (
            "; ".join(f"attempt {a['n']}: {a['cause'] or 'ok'}" for a in attempts)
            if len(attempts) > 1
            else ""
        )
        if item.get("returncode") != 0 and not detail:
            detail = str(item.get("stderr") or "")[-200:]
        checks.append(
            CheckVerdict(
                f"{item['type']}:{item['service']}",
                item.get("returncode") == 0,
                detail,
                seconds=float(item.get("seconds") or 0.0),
            )
        )
    return checks


def post_checks(hook_results: Sequence[dict]) -> list[CheckVerdict]:
    """One check for each hook slot that ran. No hook ran: one green check that says so.

    The detail starts with ``[<version_ref>]`` when the hook has a version: the guard of a
    production promotion reads it back (``staging_gates_verdict``, #1563).
    """
    if not hook_results:
        return [CheckVerdict("hooks", True, "no service with a hook slot was applied")]
    return [
        CheckVerdict(
            f"{item['service']}:{item['slot']}",
            item.get("status") == "pass",
            (
                (f"[{item['version_ref']}] " if item.get("version_ref") else "")
                + str(item.get("summary") or "")
            )[:300],
            seconds=float(item.get("duration_seconds") or 0.0),
        )
        for item in hook_results
    ]


def _cell(text: str) -> str:
    """One Markdown table cell: no pipe and no line break."""
    return " ".join(text.replace("|", "\\|").split())[:400]


def write_summary(
    *,
    records: Sequence[StageRecord],
    dry_run: bool,
    path: str | None = None,
) -> None:
    """Render the stage records of this run as the job summary. The records are the only source."""
    target = path or os.environ.get("GITHUB_STEP_SUMMARY")
    if not (target and records):
        return
    last = records[-1]
    lines = [
        "## IaC Input Reconcile",
        "",
        f"- Mode: `{'dry-run' if dry_run else 'apply'}`",
        f"- Stage: `{last.env}` `{last.subject}` ({last.event})",
        f"- Units: `{', '.join(last.units) or 'none'}`",
        f"- Uncovered by a hook: `{', '.join(last.uncovered) or 'none'}`",
    ]
    for record in records:
        lines += [
            "",
            f"### {record.phase}: {record.verdict}",
            "",
            "| check | result | seconds | detail |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| `{c.name}` | {'ok' if c.ok else 'FAILED'} | {c.seconds:g} | {_cell(c.detail)} |"
            for c in record.checks
        ]
    with open(target, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


PHASES = ("pre", "operation", "post")


@dataclass
class TrainPlan:
    """What every phase of one release run needs. Each phase builds it again from git."""

    plan: ReconcilePlan
    commands: list[DeployCommand]
    applied: list[DeployCommand]
    slots: str
    before: str


@dataclass
class PhaseOutcome:
    """What an operation or post phase returns: pass or fail, and what it ran."""

    ok: bool
    results: list[dict] = field(default_factory=list)
    hook_results: list[dict[str, object]] = field(default_factory=list)
    facts: dict[str, object] = field(default_factory=dict)


def plan_train(
    args: argparse.Namespace,
    repo_root: Path,
    *,
    before: str,
    promote_prod: bool,
    target_env: str,
) -> TrainPlan:
    """The plan, the deploy commands and the hook slots of ``args.after``."""
    changed_files = (
        list(args.changed_file)
        if args.changed_file
        else changed_files_from_git(repo_root, before or None, args.after)
    )
    tag_overrides: dict[str, str] | None = None
    if args.app_release_tag:
        tag_overrides = load_app_release_tags(
            repo_root, rev=args.after
        ) or load_app_release_tags(repo_root)
        for item in args.app_release_tag:
            if "=" not in item:
                raise SystemExit(
                    f"::error::expected SERVICE=TAG for --app-release-tag, got {item!r}"
                )
            svc, tag = item.split("=", 1)
            tag_overrides[svc.strip()] = tag.strip()
    plan = build_plan(
        changed_files,
        repo_root=repo_root,
        before=before or None,
        after=args.after,
        app_release_tags=tag_overrides,
    )
    commands = build_deploy_commands(
        plan, iac_ref=args.after, domain=args.domain, timeout=args.timeout
    )
    return TrainPlan(
        plan=plan,
        commands=commands,
        applied=commands_to_apply(
            commands,
            dry_run=args.dry_run,
            promote_prod=promote_prod,
            target_env=target_env,
        ),
        slots=args.post_deploy_hook_slot or _stage_slots_csv(target_env),
        before=before,
    )


def split_hook_slots(slots: str, target_env: str) -> tuple[str, str]:
    """``(operation slots, post slots)`` as CSV. The operation slot of the stage plan
    (``smoke``) runs in the operation phase; every other slot (the gate) runs in the post phase."""
    operation_slot = stage_hook_plan(target_env).operation_slot
    parsed = _parse_hook_slots(slots)
    return (
        ",".join(slot for slot in parsed if slot == operation_slot),
        ",".join(slot for slot in parsed if slot != operation_slot),
    )


def assert_release_ref(
    args: argparse.Namespace, repo_root: Path, target_env: str
) -> None:
    """The ref is a release tag on main. ``--dry-run`` plans only, so it may read any ref."""
    if args.dry_run:
        return
    try:
        validate_iac_ref_form(target_env, classify_ref(args.after))
    except ValueError as exc:
        raise SystemExit(f"::error::{exc}") from exc
    assert_after_on_main(args.after, repo_root)


def required_staging_gates(plan: ReconcilePlan) -> dict[str, str]:
    """``service -> release tag`` of each App whose release this promotion carries (#1563)."""
    return {
        svc: tag
        for svc in sorted(plan.selected)
        if svc in APP_SOURCES and (tag := plan.app_release_tags.get(svc, ""))
    }


def require_upstream_post(
    after: str,
    repo_root: Path,
    train: TrainPlan,
    *,
    env: Mapping[str, str] = os.environ,
) -> None:
    """Refuse a production promotion unless the records of the stage before are green (#1646).

    Three facts, each read from a post record and none from a run listing: the staging soak of
    this tag (a tag-push run, tag on main), the main CI run of the tag commit, and a green
    ``staging_gate`` of every App release since the last production marker (#1563). The check
    runs in the ``pre`` job, before the owner is asked. It prints each reading, so a refusal
    names the failed check id, and ends with ``SystemExit`` on any doubt.
    """
    repo = env.get("GITHUB_REPOSITORY", "")
    token = env.get("GITHUB_TOKEN") or env.get("GH_TOKEN", "")
    if not (repo and token):
        raise SystemExit(
            "::error::upstream_post: GITHUB_REPOSITORY and GITHUB_TOKEN are required"
        )
    commit = subprocess.run(
        ["git", "rev-parse", "--verify", f"{after}^{{commit}}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    staging_env = stage_hook_plan("staging").stage_id

    def staging(tag: str):
        return read_record(
            repo,
            token,
            env=staging_env,
            subject=f"tag:{tag}",
            phase="post",
            event="push",
        )

    readings = {
        "staging soak": staging(after),
        "main CI": read_record(
            repo, token, env=CI_ENV, subject=f"sha:{commit}", phase="post", event="push"
        ),
    }
    required = required_staging_gates(train.plan) if train.before else {}
    window: list[dict] = []
    if required:
        for tag in releases_between(train.before, after, repo_root):
            reading = readings["staging soak"] if tag == after else staging(tag)
            if reading.exit_code == EXIT_UNREADABLE:
                readings[f"staging soak {tag}"] = reading
            elif reading.ok and reading.record:
                window.append(reading.record)
    refusals = []
    for name, reading in readings.items():
        print(f"upstream_post: {name}: {reading.detail}")
        if not reading.ok:
            refusals.append(f"{name}: {reading.detail}")
    if required and not refusals:
        gates_ok, gates = staging_gates_verdict(window, required)
        print(f"upstream_post: staging_gate since {train.before}: {gates}")
        if not gates_ok:
            refusals.append(f"staging_gate: {gates}")
    if refusals:
        raise SystemExit("::error::upstream_post refused: " + " | ".join(refusals))


def _write_payload(
    args: argparse.Namespace, payload: dict[str, object], stage: "StageRun"
) -> None:
    print(json.dumps(payload, sort_keys=True))
    if args.output_json:
        Path(args.output_json).write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    write_summary(records=stage.records, dry_run=args.dry_run)


def _alert_payload(
    args: argparse.Namespace,
    job: str,
    *,
    promote_prod: bool,
    failed_checks: Sequence[CheckVerdict] = (),
    results: Sequence[Mapping[str, object]] = (),
    hook_results: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    """The part of the result file that the ``alert`` job reads."""
    return {
        "phase": job,
        "alert_text": failure_alert_text(
            after=args.after,
            results=results,
            hook_results=hook_results,
            run_url=github_run_url(os.environ),
            promote_prod=promote_prod,
            failed_job=job,
            failed_checks=failed_checks,
        ),
        "hook_results": list(hook_results),
        "results": list(results),
        "promote_prod": promote_prod,
        "dry_run": args.dry_run,
    }


def run_pre(
    args: argparse.Namespace,
    repo_root: Path,
    stage: StageRun,
    *,
    promote_prod: bool,
    target_env: str,
) -> TrainPlan:
    """Phase ``pre``: guards that change no state. A failure writes a red record and re-raises."""
    checks: list[CheckVerdict] = []
    started = utc_now()
    try:
        before = ""
        with timed_check(checks, "contract"):
            # Validate the durable desired-state coordinate before any production side
            # effect. A branch, SHA, or pre-release must never deploy successfully and
            # only then fail while trying to record its marker.
            before = plan_base(
                args.after,
                args.before,
                promote_prod=promote_prod,
                repo_root=repo_root,
                previous_tag=not args.changed_file,
            )
            if not args.dry_run:
                try:
                    validate_iac_ref_form(target_env, classify_ref(args.after))
                except ValueError as exc:
                    raise SystemExit(f"::error::{exc}") from exc
        # Fail-closed BEFORE planning/deploying: an apply run must promote a release tag
        # (a branch such as `main` is refused, #1628) that is reachable from reviewed main
        # (Infra-011). dry-run is plan-only, so it may inspect any ref.
        if not args.dry_run:
            with timed_check(checks, "provenance"):
                assert_after_on_main(args.after, repo_root)
        train = plan_train(
            args,
            repo_root,
            before=before,
            promote_prod=promote_prod,
            target_env=target_env,
        )
        stage.units = plan_units(train.plan)
        if train.applied and not args.dry_run:
            with timed_check(checks, "app_pins"):
                for cmd in train.applied:
                    is_bootstrap = "--bootstrap" in cmd.argv
                    for svc in (s.strip() for s in cmd.service.split(",") if s.strip()):
                        if svc in APP_SOURCES:
                            app_tag = train.plan.app_release_tags.get(svc, "")
                            if app_tag:
                                try:
                                    assert_app_version_ref_matches_train(
                                        svc,
                                        app_tag,
                                        cmd.deploy_type,
                                        bootstrap=is_bootstrap,
                                        iac_ref=args.after,
                                        repo_root=repo_root,
                                    )
                                    assert_app_version_ref_on_main(
                                        app_tag,
                                        cmd.deploy_type,
                                        service=svc,
                                        repo_root=repo_root,
                                    )
                                except (ValueError, RuntimeError) as exc:
                                    raise SystemExit(f"::error::{exc}") from exc
        if promote_prod and not args.dry_run:
            with timed_check(checks, "upstream_post"):
                require_upstream_post(args.after, repo_root, train)
    except BaseException as exc:
        if all(check.ok for check in checks):
            checks.append(CheckVerdict("plan", False, str(exc)[:300]))
        stage.emit("pre", started, checks)
        _write_payload(
            args,
            _alert_payload(
                args, "pre", promote_prod=promote_prod, failed_checks=checks
            ),
            stage,
        )
        raise
    stage.emit("pre", started, checks)
    return train


def run_operation(
    args: argparse.Namespace,
    repo_root: Path,
    stage: StageRun,
    train: TrainPlan,
    *,
    promote_prod: bool,
    target_env: str,
) -> PhaseOutcome:
    """Phase ``operation``: the deploy, the quick proof (``smoke``) and the production marker.

    The marker is written here, after the deploy and its ``smoke``: a ``post`` job that
    pushed it would hold ``contents: write`` and need a second owner approval.
    """
    checks: list[CheckVerdict] = []
    started = utc_now()
    results: list[dict] = []
    hook_results: list[dict[str, object]] = []
    operation_slots, post_slots = split_hook_slots(train.slots, target_env)
    if train.applied and not args.dry_run:
        try:
            with timed_check(checks, "hook_env"):
                hook_services = sorted(
                    {
                        svc
                        for cmd in train.applied
                        for svc in (s.strip() for s in cmd.service.split(","))
                        if svc in APP_SOURCES
                    }
                )
                # The `post` job of a production run holds no hook secret, so its slots are
                # audited without them: a gate that needs one fails here, before the deploy.
                post_env = {
                    k: v
                    for k, v in os.environ.items()
                    if not (promote_prod and k in HOOK_RUNNER_SECRET_NAMES)
                }
                errors: list[object] = []
                for slots, ambient in (
                    (operation_slots, os.environ),
                    (post_slots, post_env),
                ):
                    if hook_services and slots:
                        errors += audit_app_hooks(
                            repo_root,
                            services=hook_services,
                            ambient_env=ambient,
                            pinned_tags=train.plan.app_release_tags,
                            slots=_parse_hook_slots(slots),
                            target_env=target_env,
                        )["errors"]  # type: ignore[call-overload]
                if errors:
                    raise SystemExit(
                        "::error::app hook env check failed: "
                        + "; ".join(map(str, errors))
                    )
        except BaseException:
            stage.emit("operation", started, checks)
            _write_payload(
                args,
                _alert_payload(
                    args, "operation", promote_prod=promote_prod, failed_checks=checks
                ),
                stage,
            )
            raise
    if train.applied:
        results = run_deploy_commands(train.applied)
    deploy_failed = any(result.get("returncode") != 0 for result in results)
    if train.applied and not args.dry_run and not deploy_failed and operation_slots:
        hook_results = run_post_deploy_app_hooks(
            train.plan,
            train.applied,
            repo_root=repo_root,
            after=args.after,
            target_env=target_env,
            domain=args.domain,
            slot=operation_slots,
        )
    hooks_failed = any(item.get("status") != "pass" for item in hook_results)
    production_marker = ""
    marker_pending = ""
    marker_error: BaseException | None = None
    marker_checks: list[CheckVerdict] = []
    if promote_prod and not args.dry_run and not deploy_failed and not hooks_failed:
        marker_began = time.monotonic()
        try:
            production_marker = record_production_marker(args.after, repo_root)
            detail = production_marker
        except ProductionMarkerRejected as exc:
            marker_pending = production_marker_tag(args.after)
            detail = f"pending: {exc}"
            print(
                f"::warning::production marker {marker_pending} is pending: GitHub "
                "rejects a GitHub App push for a commit whose workflow files differ "
                "from main (#1511). Record it by hand: see docs/ssot/ops.pipeline.md, "
                "section on the production marker."
            )
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - record_production_marker fails with SystemExit; the record is written first, then it re-raises
            marker_error = exc
            detail = f"{type(exc).__name__}: {exc}"
        marker_checks.append(
            CheckVerdict(
                "production_marker",
                bool(production_marker),
                detail[:300],
                seconds=round(time.monotonic() - marker_began, 3),
            )
        )
    stage.emit(
        "operation",
        started,
        [
            *checks,
            *operation_checks(results, dry_run=args.dry_run),
            *(post_checks(hook_results) if hook_results else []),
            *marker_checks,
        ],
    )
    facts: dict[str, object] = {
        "plan": train.plan.to_dict(),
        "commands": [command.to_dict() for command in train.commands],
        # what actually ran this invocation vs what is left for a deliberate next step
        # (prod promotion, or — in dry-run — everything is just a plan).
        "applied": [command.to_dict() for command in train.applied],
        "deferred": [
            command.to_dict()
            for command in train.commands
            if command not in train.applied
        ],
        "target_env": target_env,
        "production_marker": production_marker,
        "marker_pending": marker_pending,
    }
    if marker_error is not None:
        _write_payload(
            args,
            _alert_payload(
                args,
                "operation",
                promote_prod=promote_prod,
                results=results,
                hook_results=hook_results,
            )
            | facts,
            stage,
        )
        raise marker_error
    return PhaseOutcome(
        not (deploy_failed or hooks_failed), results, hook_results, facts
    )


def run_post(
    args: argparse.Namespace,
    repo_root: Path,
    stage: StageRun,
    train: TrainPlan,
    *,
    promote_prod: bool,
    target_env: str,
) -> PhaseOutcome:
    """Phase ``post``: the gate hooks (``staging_gate`` or ``prod_gate``). It never deploys,
    so it can run again alone on a tag. It writes only its record."""
    started = utc_now()
    _, post_slots = split_hook_slots(train.slots, target_env)
    hook_results: list[dict[str, object]] = []
    if train.applied and not args.dry_run and post_slots:
        hook_results = run_post_deploy_app_hooks(
            train.plan,
            train.applied,
            repo_root=repo_root,
            after=args.after,
            target_env=target_env,
            domain=args.domain,
            slot=post_slots,
        )
    if not args.dry_run:
        hooked = {str(item["service"]) for item in hook_results}
        stage.emit(
            "post",
            started,
            post_checks(hook_results),
            uncovered=[
                unit
                for unit in stage.units
                if unit.split(":", 1)[0] in ("app", "infra")
                and unit.split(":", 1)[1] not in hooked
            ],
        )
    return PhaseOutcome(
        not any(item.get("status") != "pass" for item in hook_results),
        hook_results=hook_results,
    )


def run_train(
    args: argparse.Namespace,
    repo_root: Path,
    *,
    promote_prod: bool,
    target_env: str,
) -> int:
    """Run the phases of ``--phase`` for ``args.after``; the exit code is 1 when one fails."""
    phases = PHASES if args.phase == "all" else (args.phase,)
    stage = StageRun(
        env=stage_hook_plan(target_env).stage_id,
        subject=f"tag:{args.after}",
        dry_run=args.dry_run,
    )
    kwargs = {"promote_prod": promote_prod, "target_env": target_env}
    if "pre" in phases:
        train = run_pre(args, repo_root, stage, **kwargs)
    else:
        # operation and post start from the same two guards that ``pre`` ran.
        assert_release_ref(args, repo_root, target_env)
        before = plan_base(
            args.after,
            args.before,
            promote_prod=promote_prod,
            repo_root=repo_root,
            previous_tag=not args.changed_file,
        )
        train = plan_train(args, repo_root, before=before, **kwargs)
        stage.units = plan_units(train.plan)
    results: list[dict] = []
    hook_results: list[dict[str, object]] = []
    facts: dict[str, object] = {"plan": train.plan.to_dict(), "target_env": target_env}
    failed_job = ""
    for name, run in (("operation", run_operation), ("post", run_post)):
        if name not in phases or failed_job:
            continue
        outcome = run(args, repo_root, stage, train, **kwargs)
        results += outcome.results
        hook_results += outcome.hook_results
        facts |= outcome.facts
        failed_job = "" if outcome.ok else name
    payload = _alert_payload(
        args,
        failed_job or phases[-1],
        promote_prod=promote_prod,
        results=results,
        hook_results=hook_results,
    )
    if not failed_job:
        payload["alert_text"] = None
    _write_payload(args, payload | facts, stage)
    return 1 if failed_job else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="reconcile changed IaC service inputs")
    parser.add_argument("--repo-root", default=".", help="git repository root")
    parser.add_argument(
        "--before",
        default="",
        help="previous release tag (base for changed-file detection)",
    )
    parser.add_argument("--after", default="", help="release tag to promote (iac_ref)")
    parser.add_argument(
        "--domain", default=os.environ.get("INTERNAL_DOMAIN", "zitian.party")
    )
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument(
        "--dry-run", action="store_true", help="plan only; do not deploy"
    )
    parser.add_argument(
        "--env",
        dest="target_env",
        choices=("staging", "prod", "production"),
        default=None,
        help="target environment to reconcile ('staging' or 'prod'); default is staging (#940)",
    )
    parser.add_argument(
        "--promote-prod",
        action="store_true",
        help=(
            "explicitly promote prod (production deploys). Default applies STAGING "
            "only; prod is a separate, deliberate promotion — a tag push never "
            "auto-deploys prod."
        ),
    )
    parser.add_argument(
        "--changed-file",
        action="append",
        default=[],
        help="explicit changed file; bypasses git diff when provided",
    )
    parser.add_argument(
        "--app-release-tag",
        action="append",
        default=[],
        metavar="SERVICE=TAG",
        help="override App release_tag for planning (repeatable, e.g. finance_report/app=v0.2.46)",
    )
    parser.add_argument(
        "--run-hook-slot",
        default="",
        help="optional verification hook slot to execute via infra2_sdk.manifests",
    )
    parser.add_argument(
        "--hooks-manifest",
        default="",
        help="path to runtime-hooks.manifest.json when --run-hook-slot is set",
    )
    parser.add_argument(
        "--hook-service",
        default="",
        help="App service key to fetch runtime-hooks.manifest.json for when --hooks-manifest is omitted",
    )
    parser.add_argument(
        "--hook-source-sha",
        default="",
        help="40-hex commit sha for fetching runtime-hooks.manifest.json",
    )
    parser.add_argument(
        "--hook-app-url",
        default="",
        help="App URL passed to --run-hook-slot",
    )
    parser.add_argument(
        "--query-serving-service",
        default="",
        help="optional App service key to query serving release state for",
    )
    parser.add_argument(
        "--validate-evidence-json",
        default="",
        help="optional DeployRequest JSON string to validate evidence URLs for",
    )
    parser.add_argument(
        "--advance-train-service",
        default="",
        help="optional App service key to compute next release train tag for",
    )
    parser.add_argument(
        "--advance-train-repo",
        default="",
        help="Git repo URL for --advance-train-service",
    )
    parser.add_argument(
        "--advance-train-tag",
        action="append",
        default=[],
        help="existing release tag for --advance-train-service (repeatable)",
    )
    parser.add_argument(
        "--post-deploy-hook-slot",
        default="",
        help=(
            "comma-separated hook slot(s) from HOOK_SLOTS to run when "
            "post-deploy hooks run (defaults to 'smoke,staging_gate' "
            "for staging and 'smoke,prod_gate' for prod via STAGE_HOOK_PLANS; #1608)"
        ),
    )
    parser.add_argument(
        "--print-changed-files",
        action="store_true",
        help=(
            "print the files that changed between the base release and --after, one per "
            "line; the base is the production marker for --promote-prod (#1646)"
        ),
    )
    parser.add_argument(
        "--phase",
        choices=("all", *PHASES),
        default="all",
        help=(
            "run one phase of the release train: pre (guards and the upstream record), "
            "operation (deploy, smoke, marker) or post (the gate hooks, no deploy). "
            "The default runs the three in order (#1646)"
        ),
    )
    parser.add_argument(
        "--run-scheduled-audit",
        action="store_true",
        help=(
            "execute the scheduled_audit runtime hook slot across registered Apps (#1493)"
        ),
    )
    parser.add_argument(
        "--target-url",
        default="",
        help="explicit target URL override when running --run-hook-slot (e.g. preview_gate)",
    )
    parser.add_argument(
        "--version-ref",
        default="",
        help="explicit version ref override when running --run-hook-slot",
    )
    parser.add_argument(
        "--sync-app-releases",
        action="store_true",
        help="query upstream App repositories and sync harness/repos.yaml release_tag pins (#1416)",
    )
    parser.add_argument(
        "--sync-service",
        action="append",
        default=[],
        help="limit --sync-app-releases to specific App service(s) (repeatable)",
    )
    parser.add_argument(
        "--apply-repos-yaml",
        action="store_true",
        help="write updated release_tag values to harness/repos.yaml during --sync-app-releases",
    )
    parser.add_argument(
        "--sync-gitlinks",
        action="store_true",
        help="update submodule gitlinks alongside harness/repos.yaml during --sync-app-releases",
    )
    parser.add_argument(
        "--cut-missing-app-tag",
        action="store_true",
        help="compute next patch tag when upstream App main is ahead of its latest v*.*.* tag",
    )
    parser.add_argument(
        "--create-pr",
        action="store_true",
        help="push updated harness/repos.yaml to a branch and open/update a PR (#1416)",
    )
    parser.add_argument(
        "--pr-branch",
        default="chore/sync-app-releases",
        help="head branch name when --create-pr is enabled (default: chore/sync-app-releases)",
    )
    parser.add_argument(
        "--audit-app-hooks",
        action="store_true",
        help="validate 5-slot schema v2 runtime-hooks.manifest.json contracts for registered Apps (#1608)",
    )
    parser.add_argument("--output-json", default="", help="optional output JSON path")
    args = parser.parse_args(argv)
    promote_prod = bool(args.promote_prod or args.target_env in ("prod", "production"))
    target_env = "prod" if promote_prod else "staging"

    repo_root = Path(args.repo_root).resolve()
    if args.run_scheduled_audit:
        hook_results = run_scheduled_app_audits(
            repo_root,
            target_env=target_env,
            slot="scheduled_audit",
            base_domain=args.domain,
            services=args.sync_service or None,
        )
        out_payload = {"scheduled_audit_results": hook_results}
        print(json.dumps(out_payload, sort_keys=True))
        if args.output_json:
            Path(args.output_json).write_text(
                json.dumps(out_payload, indent=2), encoding="utf-8"
            )
        return 1 if any(item.get("status") != "pass" for item in hook_results) else 0
    if args.audit_app_hooks:
        audit_res = audit_app_hooks(
            repo_root,
            services=args.sync_service,
        )
        print(json.dumps(audit_res, sort_keys=True))
        if args.output_json:
            Path(args.output_json).write_text(
                json.dumps(audit_res, indent=2), encoding="utf-8"
            )
        return 0 if audit_res.get("ok") else 1
    if args.sync_app_releases:
        sync_res = sync_app_releases(
            repo_root,
            services=args.sync_service,
            apply_repos_yaml=args.apply_repos_yaml,
            sync_gitlinks=args.sync_gitlinks,
            cut_missing_app_tag=args.cut_missing_app_tag,
            create_pr=args.create_pr,
            pr_branch=args.pr_branch,
            dry_run=args.dry_run,
        )
        print(json.dumps(sync_res, sort_keys=True))
        if args.output_json:
            Path(args.output_json).write_text(
                json.dumps(sync_res, indent=2), encoding="utf-8"
            )
        return 0
    if args.advance_train_service:
        train = advance_app_release_train(
            args.advance_train_service,
            repo_url=args.advance_train_repo,
            existing_tags=args.advance_train_tag,
            candidate_ref=args.after or "main",
        )
        print(json.dumps(train, sort_keys=True))
        return 0
    if args.validate_evidence_json:
        request = DeployRequest.from_dict(json.loads(args.validate_evidence_json))
        evidence_result = validate_app_release_evidence(
            request, verify_prod=promote_prod
        )
        print(json.dumps(evidence_result, sort_keys=True))
        return 0
    if args.query_serving_service:
        serving = query_serving_release(
            args.query_serving_service,
            target_env,
            domain=args.domain,
        )
        print(json.dumps(serving.to_dict(), sort_keys=True))
        return 0
    if args.run_hook_slot:
        if args.run_hook_slot not in HOOK_SLOTS:
            raise SystemExit(
                f"::error::unknown hook slot {args.run_hook_slot!r}; allowed: {list(HOOK_SLOTS)}"
            )
        version_ref = args.version_ref or args.after or "main"
        explicit_url = args.target_url or args.hook_app_url
        app_url = explicit_url or f"https://{args.domain}"
        hook_cwd = repo_root
        tmp_checkout = None
        local_manifests = _discover_local_service_hook_manifests(repo_root)
        rel_local = (
            (
                local_manifests.get(args.hook_service)
                or LOCAL_SERVICE_HOOK_MANIFESTS.get(args.hook_service)
            )
            if args.hook_service
            else None
        )
        if args.hooks_manifest:
            manifest_or_path = Path(args.hooks_manifest)
            if args.hook_service and not explicit_url:
                if args.hook_service not in APP_SOURCES and rel_local:
                    app_url = _local_service_app_url(
                        args.hook_service,
                        "prod" if promote_prod else target_env,
                        args.domain,
                    )
                else:
                    resolved_ver, _, app_url = resolve_app_hook_coordinates(
                        args.hook_service,
                        repo_root=repo_root,
                        after=args.version_ref or args.after,
                        target_env=target_env,
                        domain=args.domain,
                        source_sha=args.hook_source_sha or ("a" * 40),
                    )
                    version_ref = args.version_ref or resolved_ver
        elif args.hook_service and args.hook_service not in APP_SOURCES and rel_local:
            local_path = Path(repo_root) / rel_local
            if not local_path.is_file():
                local_path = REPO_ROOT / rel_local
            manifest_or_path = load_hooks_manifest(local_path)
            version_ref = args.version_ref or args.after or "main"
            app_url = explicit_url or _local_service_app_url(
                args.hook_service,
                "prod" if promote_prod else target_env,
                args.domain,
            )
            hook_cwd = local_path.parent
        elif args.hook_service and args.hook_source_sha:
            if not explicit_url:
                resolved_ver, _, app_url = resolve_app_hook_coordinates(
                    args.hook_service,
                    repo_root=repo_root,
                    after=args.version_ref or args.after,
                    target_env=target_env,
                    domain=args.domain,
                    source_sha=args.hook_source_sha,
                )
                version_ref = args.version_ref or resolved_ver
            manifest_or_path = fetch_hooks_manifest(
                args.hook_service, args.hook_source_sha
            )
            hook_cwd, tmp_checkout = resolve_app_hook_cwd(
                args.hook_service,
                args.hook_source_sha,
                release_ref=_slot_release_ref(
                    manifest_or_path, (args.run_hook_slot,), version_ref
                ),
                repo_root=repo_root,
            )
        elif args.hook_service:
            resolved_ver, resolved_sha, resolved_url = resolve_app_hook_coordinates(
                args.hook_service,
                repo_root=repo_root,
                after=args.version_ref or args.after,
                target_env=target_env,
                domain=args.domain,
                app_url=explicit_url,
            )
            version_ref = args.version_ref or resolved_ver
            app_url = explicit_url or resolved_url
            manifest_or_path = fetch_hooks_manifest(args.hook_service, resolved_sha)
            hook_cwd, tmp_checkout = resolve_app_hook_cwd(
                args.hook_service,
                resolved_sha,
                release_ref=_slot_release_ref(
                    manifest_or_path, (args.run_hook_slot,), version_ref
                ),
                repo_root=repo_root,
            )
        else:
            raise SystemExit(
                "::error::--run-hook-slot requires either --hooks-manifest or both "
                "--hook-service and --hook-source-sha"
            )
        try:
            hook_res = execute_app_hook_slot(
                manifest_or_path,
                args.run_hook_slot,
                app_url=app_url,
                environment="production" if promote_prod else "staging",
                version_ref=version_ref,
                cwd=hook_cwd,
            )
        finally:
            if tmp_checkout is not None:
                tmp_checkout.cleanup()
        print(json.dumps(hook_res.to_dict(), sort_keys=True))
        return 0 if hook_res.status == "pass" else 1

    if not args.after:
        raise SystemExit("::error::--after is required for IaC input reconcile")
    if args.print_changed_files:
        base = plan_base(
            args.after,
            args.before,
            promote_prod=promote_prod,
            repo_root=repo_root,
            previous_tag=not args.changed_file,
        )
        print("\n".join(changed_files_from_git(repo_root, base or None, args.after)))
        return 0
    return run_train(args, repo_root, promote_prod=promote_prod, target_env=target_env)


if __name__ == "__main__":
    raise SystemExit(main())
