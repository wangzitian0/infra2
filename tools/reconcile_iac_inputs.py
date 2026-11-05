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
from collections.abc import Mapping
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence
import yaml

from common.core.constants import APP_SOURCES, REPO_ROOT
from common.core.registry import domain_for_service
from common.deploy.app_contract import (
    APP_HOOKS_MANIFEST_PATH,
    HOOK_SLOTS,
    RuntimeHooksManifest,
    execute_app_hook_slot,
    fetch_hooks_manifest,
    git_url,
    load_hooks_manifest,
    next_app_release_tag,
    query_serving_release,
    validate_app_release_evidence,
)
from common.deploy.dependencies import explain_fanout
from common.deploy.contract import all_service_keys, service_spec
from common.deploy.env_config import app_compose_env_config
from common.deploy.transient_failure import (
    classify_failure,
    rolled_back,
    rollout_started,
)
from common.deploy.release_markers import (
    PRODUCTION_MARKER_PREFIX,
    RELEASE_TAG_RE,
    newest_release_tag,
    version_key,
)
from common.observability.issue_trail import (
    CheckVerdict,
    GitHubIssues,
    Trail,
    issue_trail_mode,
    reconcile_issue_trail,
)
from infra2_sdk.deploy import DeployRequest
from infra2_sdk.manifests import (
    FORBIDDEN_HOOK_ENV_NAMES,
    FORBIDDEN_HOOK_ENV_PREFIXES,
    build_hook_env,
)
from infra2_sdk.refs import (
    assert_after_on_main,  # noqa: F401  (re-exported; #616)
    next_patch_tag,
    resolve_image_ref,
)

MANIFEST_PATH = "docs/ssot/deploy-dependencies.yaml"
HARNESS_REPOS_PATH = "harness/repos.yaml"
APP_REPO_ID_TO_SERVICE: dict[str, str] = {
    "finance-report": "finance_report/app",
    "truealpha": "truealpha/app",
}
SERVICE_TO_APP_REPO_ID: dict[str, str] = {
    service: repo_id for repo_id, service in APP_REPO_ID_TO_SERVICE.items()
}
SERVICE_TO_SUBMODULE_PATH: dict[str, str] = {
    "finance_report/app": "repos/finance_report",
    "truealpha/app": "repos/truealpha",
}
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


def assert_production_marker_advances(
    release_tag: str,
    repo_root: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str | None:
    """Validate marker history and return the current production release, if any."""
    production_marker_tag(release_tag)
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
    candidates = [m.removeprefix(PRODUCTION_MARKER_PREFIX) for m in markers]
    if not candidates:
        return None

    current = max(candidates, key=version_key)
    if version_key(release_tag) < version_key(current):
        raise SystemExit(
            f"::error::production promotion cannot move backward from {current} "
            f"to {release_tag}; publish a newer revert release instead"
        )
    return current


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
        raise SystemExit(
            f"::error::cannot push production marker {marker}: {pushed.stderr.strip()}"
        )
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


def sync_app_releases(
    repo_root: Path = REPO_ROOT,
    *,
    services: Sequence[str] = (),
    apply_repos_yaml: bool = False,
    sync_gitlinks: bool = False,
    cut_missing_app_tag: bool = False,
    cut_infra_tag: bool = False,
    push_infra_tag: bool = False,
    create_pr: bool = False,
    pr_branch: str = "chore/sync-app-releases",
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict[str, object]:
    """Pull-based App version sync against remote App repositories (#1416)."""
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
        if cut_missing_app_tag and main_ahead and computed_next_tag:
            target_tag = computed_next_tag
            target_sha = main_sha

        drifted = bool(target_tag and target_tag != pinned_tag)
        if drifted:
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
            "needs_update": drifted,
        }

    repos_yaml_updated = False
    gitlinks_updated: list[str] = []
    next_infra_tag = ""
    infra_tag_cut = False
    infra_tag_pushed = False
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
                pr_updated = True
            else:
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
                pr_url = (created_pr.stdout or "").strip()
                m_num = re.search(r"/pull/([0-9]+)$", pr_url)
                if m_num:
                    pr_number = int(m_num.group(1))
                pr_created = True
        elif cut_infra_tag and repos_yaml_updated and not dry_run:
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
                ["git", "tag", next_infra_tag],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True,
            )
            infra_tag_cut = True
            if push_infra_tag:
                runner(
                    [
                        "git",
                        "push",
                        "origin",
                        "HEAD:main",
                        f"refs/tags/{next_infra_tag}",
                    ],
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                infra_tag_pushed = True

    return {
        "apps": entries,
        "updates": updates,
        "needs_update": bool(updates),
        "repos_yaml_updated": repos_yaml_updated,
        "gitlinks_updated": gitlinks_updated,
        "next_infra_tag": next_infra_tag,
        "infra_tag_cut": infra_tag_cut,
        "infra_tag_pushed": infra_tag_pushed,
        "pr_branch": pr_branch if create_pr else "",
        "pr_created": pr_created,
        "pr_updated": pr_updated,
        "pr_url": pr_url,
        "pr_number": pr_number,
        "dry_run": dry_run,
    }


def resolve_app_hook_cwd(
    service: str,
    source_sha: str,
    *,
    repo_root: Path = REPO_ROOT,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
    """Return ``(app_cwd, temp_dir_or_none)`` containing an App checkout at ``source_sha`` (#1416)."""
    sub_rel = SERVICE_TO_SUBMODULE_PATH.get(service, "")
    if sub_rel:
        sub_dir = Path(repo_root) / sub_rel
        if sub_dir.is_dir() and (sub_dir / APP_HOOKS_MANIFEST_PATH).is_file():
            return sub_dir, None
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
        return checkout_dir, tmp_dir
    except Exception:
        tmp_dir.cleanup()
        return Path(repo_root), None


def record_app_hook_issue_trail(
    hook_results: Sequence[Mapping[str, object]],
    *,
    target_env: str = "staging",
    run_url: str = "",
    env: Mapping[str, str] | None = None,
    issues_factory: Callable[[str, str], object] = GitHubIssues,
) -> int:
    """Open, comment on, or close an incident issue in the target App repository (#1416)."""
    current_env = dict(os.environ if env is None else env)
    token = (
        current_env.get("GITHUB_TOKEN") or current_env.get("GH_TOKEN") or ""
    ).strip()
    if not token or not hook_results:
        return 0
    mode = issue_trail_mode(current_env)
    if mode == "off":
        effective_env = {**current_env, "GITHUB_EVENT_NAME": "workflow_dispatch"}
        mode = issue_trail_mode(effective_env)
    else:
        effective_env = current_env

    by_service: dict[str, list[Mapping[str, object]]] = {}
    for entry in hook_results:
        svc = str(entry.get("service") or "").strip()
        if svc:
            by_service.setdefault(svc, []).append(entry)

    rc = 0
    norm_env = "production" if target_env in ("prod", "production") else "staging"
    for svc, items in sorted(by_service.items()):
        app_repo = APP_SOURCES.get(
            svc, current_env.get("GITHUB_REPOSITORY", "wangzitian0/infra2")
        )
        trail = Trail(complete=False)
        for item in items:
            slot = str(item.get("slot") or "smoke")
            status = str(item.get("status") or "fail")
            name = f"app-hook:{svc}:{slot}:{norm_env}"
            if status == "pass":
                trail.green.add(name)
            else:
                summary = str(item.get("summary") or f"{slot} failed")
                stderr_tail = str(item.get("stderr_tail") or "")
                app_url = str(item.get("app_url") or "")
                version_ref = str(item.get("version_ref") or "")
                detail = (
                    f"service={svc} env={norm_env} version_ref={version_ref} "
                    f"app_url={app_url}\n{summary}\n{stderr_tail}"
                ).strip()
                trail.failing[name] = CheckVerdict(
                    name=name,
                    ok=False,
                    detail=detail,
                    severity="high",
                    failure_domain="app",
                )
        res = reconcile_issue_trail(
            issues_factory(app_repo, token),
            trail,
            mode=mode,
            run_url=run_url or github_run_url(current_env),
            context=f"post-deploy hook ({norm_env})",
            env=effective_env,
        )
        if res != 0:
            fallback_repo = current_env.get("GITHUB_REPOSITORY", "wangzitian0/infra2")
            if fallback_repo and fallback_repo != app_repo:
                res = reconcile_issue_trail(
                    issues_factory(fallback_repo, token),
                    trail,
                    mode=mode,
                    run_url=run_url or github_run_url(current_env),
                    context=f"post-deploy hook ({norm_env})",
                    env=effective_env,
                )
            rc = rc or res
    return rc


def audit_app_hooks(
    repo_root: Path = REPO_ROOT,
    *,
    services: Sequence[str] = (),
    manifest_fetcher: Callable[[str], bytes] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict[str, object]:
    """Validate 4-slot ``runtime-hooks.manifest.json`` contracts for all registered Apps (#1416)."""
    target_services = (
        [str(s) for s in services if str(s)] if services else sorted(APP_SOURCES.keys())
    )
    pinned_tags = load_app_release_tags(repo_root, runner=runner)
    checked: dict[str, dict[str, object]] = {}
    errors: list[str] = []

    for service in target_services:
        repo_slug = APP_SOURCES.get(service)
        if not repo_slug:
            errors.append(f"unknown app service {service!r}")
            continue
        pinned_tag = pinned_tags.get(service, "")
        sub_rel = SERVICE_TO_SUBMODULE_PATH.get(service, "")
        local_manifest_path = (
            (Path(repo_root) / sub_rel / APP_HOOKS_MANIFEST_PATH) if sub_rel else None
        )
        manifest: RuntimeHooksManifest | None = None
        source = ""
        try:
            if (
                manifest_fetcher is None
                and local_manifest_path is not None
                and local_manifest_path.is_file()
            ):
                manifest = load_hooks_manifest(local_manifest_path)
                source = str(local_manifest_path.relative_to(repo_root))
            else:
                ref = pinned_tag or "main"
                resolved = resolve_image_ref(
                    ref, repo=git_url(repo_slug), runner=runner
                )
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

        slot_errors: list[str] = []
        for slot_name in HOOK_SLOTS:
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
        for dry_slot in ("smoke", "scheduled_audit"):
            spec = manifest.slots[dry_slot]
            augmented_opt = tuple(
                dict.fromkeys(
                    [
                        *spec.optional_env,
                        "INFRA2_HOOK_APP_URL",
                        "BASE_URL",
                        "DEPLOY_ENV",
                    ]
                )
            )
            ambient = {
                "APP_URL": "https://staging.example.invalid",
                "INFRA2_HOOK_APP_URL": "https://staging.example.invalid",
                "BASE_URL": "https://staging.example.invalid",
                "DEPLOY_BASE_URL": "https://staging.example.invalid",
                "STAGING_BASE_URL": "https://staging.example.invalid",
                "TA_BASE_URL": "https://staging.example.invalid",
                "ENVIRONMENT": "staging",
                "DEPLOY_ENV": "staging",
                "APP_VERSION_REF": pinned_tag or "v0.1.0",
                "EXPECTED_SHA": pinned_tag or "v0.1.0",
            }
            for req_key in spec.required_env:
                ambient.setdefault(req_key, "dry-run-placeholder")
            try:
                build_hook_env(
                    replace(spec, optional_env=augmented_opt),
                    slot=dry_slot,
                    app_url="https://staging.example.invalid",
                    environment="staging",
                    version_ref=pinned_tag or "v0.1.0",
                    artifacts_dir=spec.artifacts_dir,
                    ambient_env=ambient,
                )
            except ValueError as exc:
                slot_errors.append(f"{dry_slot}: {exc}")
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
    resolved_url = app_url.strip()
    if not resolved_url:
        effective_domain = domain_for_service(service) or domain
        norm_env = "prod" if target_env in ("prod", "production") else "staging"
        cfg = app_compose_env_config(service, norm_env)
        resolved_url = cfg.app_url(domain=effective_domain)
    return version_ref, resolved_sha, resolved_url


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
    record_issues: bool = True,
) -> list[dict[str, object]]:
    """Execute post-deploy verification hook slot for each deployed App service (#1416)."""
    applied_services = {
        part.strip()
        for cmd in applied
        for part in cmd.service.split(",")
        if part.strip() in APP_SOURCES
    }
    hook_results: list[dict[str, object]] = []
    for service in sorted(applied_services):
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
            repo_root=repo_root,
            runner=runner,
        )
        try:
            res = execute_app_hook_slot(
                manifest,
                slot,
                app_url=app_url,
                environment="production"
                if target_env in ("prod", "production")
                else "staging",
                version_ref=version_ref,
                cwd=hook_cwd,
                runner=runner,
            )
        finally:
            if tmp_checkout is not None:
                tmp_checkout.cleanup()
        entry = res.to_dict()
        entry["service"] = service
        entry["app_url"] = app_url
        entry["source_sha"] = source_sha
        entry["cwd"] = str(hook_cwd)
        hook_results.append(entry)
        if res.status != "pass":
            break
    if (
        record_issues
        and hook_results
        and (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    ):
        record_app_hook_issue_trail(
            hook_results,
            target_env=target_env,
            run_url=github_run_url(os.environ),
            env=os.environ,
        )
    return hook_results


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
# automatic retries of a failed unit, in the same run and on the same tag. A retry runs the
# same command line again, so it can neither start a new run nor change the tag.
MAX_UNIT_RETRIES = 2
# The pause before retry 1 and retry 2. A resolver or a registry needs seconds to recover;
# a restarted runner needs about a minute to answer again.
UNIT_RETRY_BACKOFF_SECONDS = (30.0, 90.0)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_deploy_commands(
    commands: Sequence[DeployCommand],
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], str] = _utc_now,
    max_retries: int = MAX_UNIT_RETRIES,
    backoff_seconds: Sequence[float] = UNIT_RETRY_BACKOFF_SECONDS,
) -> list[dict]:
    """Run each deploy command; retry a failed one on a transient fault, then stop at a
    failure that stays.

    A failed command is retried at most ``max_retries`` times, and only when
    :func:`common.deploy.transient_failure.classify_failure` calls its failure transient
    (DNS or network, a registry 5xx, a runner that lost or interrupted the deploy). A
    deterministic or unrecognised failure is never retried. Every attempt, the first
    included, is recorded as ``attempts: [{n, cause, at}]``: ``cause`` is why that attempt
    failed, and ``None`` for an attempt that succeeded. Each retry is also logged to
    stderr with its cause.

    Idempotency: the command for a second attempt is the first attempt's command line. A
    platform unit is a signed ``/deploy`` request with the same coordinates; the runner
    answers a finished deploy from its record, and the config-hash gate skips a service that
    already matches (only when its newest Dokploy deployment record is ``done``). An app
    unit has no such gate and its failure triggers the automatic rollback, which a re-run
    would repeat until the circuit breaker trips. So an app unit is retried only when it
    failed before its first write to the compose: ``classify_failure`` refuses any attempt
    whose stderr holds the rollout marker, and each result records ``rollout_started`` and
    ``rolled_back``.
    """
    results: list[dict] = []
    for command in commands:
        attempts: list[dict] = []
        while True:
            attempt = {"n": len(attempts) + 1, "cause": None, "at": clock()}
            attempts.append(attempt)
            completed = runner(
                command.argv,
                capture_output=True,
                text=True,
            )
            if completed.returncode == 0:
                break
            failure = classify_failure(completed.stderr, completed.stdout)
            attempt["cause"] = failure.cause
            retries_used = attempt["n"] - 1
            if not failure.transient or retries_used >= max_retries:
                break
            pause = float(backoff_seconds[min(retries_used, len(backoff_seconds) - 1)])
            print(
                f"reconcile retry: {command.deploy_type} {command.service}: attempt "
                f"{attempt['n'] + 1} of {max_retries + 1} after {failure.cause} "
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
            # Did the last attempt write to the compose, and did it roll back? An app unit
            # that did either is not retried (transient_failure, #1439).
            "rollout_started": rollout_started(completed.stderr),
            "rolled_back": rolled_back(completed.stderr),
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


def failure_summary_from_result(result: dict) -> list[dict]:
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


def failure_alert_text(
    *, after: str, results: Sequence[dict], run_url: str, promote_prod: bool
) -> str | None:
    """One Feishu message for a failed reconcile: which tag, which environment, and per
    service the runner's error_kind and next action — the earliest machine-readable point
    of every deploy-chain incident of 2026-09-04..08 (infra2#658). ``None`` when nothing
    failed."""
    failed = [item for item in results if item.get("returncode") != 0]
    if not failed:
        return None
    lines = [
        f"🚨 IaC reconcile FAILED for {after} ({'PRODUCTION promotion' if promote_prod else 'staging soak'})."
    ]
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
                line for line in stderr_lines if line.startswith("deploy_v2 failed:")
            ]
            detail = (verdict or stderr_lines or ["no diagnostic"])[-1]
            lines.append(f"- {env} {services}: {detail[:200]}")
    for item in failed:
        attempts = item.get("attempts") or []
        if len(attempts) > 1:
            lines.append(
                f"- {item.get('type') or '?'} {item.get('service') or '?'}: "
                f"still failing after {len(attempts)} attempts "
                f"(last cause: {attempts[-1].get('cause') or 'unknown'})"[:300]
            )
    lines.append(f"Run: {run_url}")
    return "\n".join(lines)


def write_summary(
    *,
    plan: ReconcilePlan,
    commands: Sequence[DeployCommand],
    results: Sequence[dict],
    dry_run: bool,
    path: str | None = None,
) -> None:
    target = path or os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return
    lines = [
        "## IaC Input Reconcile",
        "",
        f"- Mode: `{'dry-run' if dry_run else 'apply'}`",
        f"- Changed files: `{len(plan.changed_files)}`",
        f"- Selected services: `{', '.join(plan.services) if plan.services else 'none'}`",
        f"- Dropped files: `{len(plan.dropped)}`",
        f"- Commands: `{len(commands)}`",
    ]
    if plan.ignored:
        lines.append(
            "- Ignored services: "
            + ", ".join(
                f"`{service}` ({reason})" for service, reason in plan.ignored.items()
            )
        )
    if plan.not_yet_in_production:
        lines.append(
            "- Staging-only (not yet in production): "
            + ", ".join(f"`{service}`" for service in plan.not_yet_in_production)
        )
    if results:
        failed = [item for item in results if item.get("returncode") != 0]
        lines.append(
            f"- Deploy results: `{len(results) - len(failed)} ok / {len(failed)} failed`"
        )
        retried = [item for item in results if len(item.get("attempts") or []) > 1]
        if retried:
            lines.append(
                f"- Unit retries (at most {MAX_UNIT_RETRIES} per unit, same run, same tag):"
            )
            for item in retried:
                steps = "; ".join(
                    f"attempt {a.get('n')} at {a.get('at')}: {a.get('cause') or 'ok'}"
                    for a in item["attempts"]
                )
                lines.append(
                    f"  - `{item.get('service')}` ({item.get('type')}): {steps}"
                )
    lines.append("")
    lines.append("```json")
    lines.append(
        json.dumps({"plan": plan.to_dict(), "results": list(results)}, indent=2)
    )
    lines.append("```")
    with open(target, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


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
        "--run-post-deploy-hooks",
        action="store_true",
        help="execute post-deploy App smoke hooks after successful deploy_v2 commands (#1416)",
    )
    parser.add_argument(
        "--post-deploy-hook-slot",
        default="smoke",
        help="hook slot to run when --run-post-deploy-hooks is enabled (default: smoke)",
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
        "--cut-infra-tag",
        action="store_true",
        help="commit updated harness/repos.yaml and create next infra2 vX.Y.Z tag when updates exist",
    )
    parser.add_argument(
        "--push-infra-tag",
        action="store_true",
        help="push committed harness/repos.yaml and next infra2 vX.Y.Z tag to origin",
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
        help="validate 4-slot runtime-hooks.manifest.json contracts for registered Apps (#1416)",
    )
    parser.add_argument("--output-json", default="", help="optional output JSON path")
    args = parser.parse_args(argv)
    promote_prod = bool(args.promote_prod or args.target_env in ("prod", "production"))
    target_env = "prod" if promote_prod else "staging"

    repo_root = Path(args.repo_root).resolve()
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
            cut_infra_tag=args.cut_infra_tag,
            push_infra_tag=args.push_infra_tag,
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
        version_ref = args.after or "main"
        app_url = args.hook_app_url or f"https://{args.domain}"
        hook_cwd = repo_root
        tmp_checkout = None
        if args.hooks_manifest:
            manifest_or_path = Path(args.hooks_manifest)
            if args.hook_service and not args.hook_app_url:
                version_ref, _, app_url = resolve_app_hook_coordinates(
                    args.hook_service,
                    repo_root=repo_root,
                    after=args.after,
                    target_env=target_env,
                    domain=args.domain,
                    source_sha=args.hook_source_sha or ("a" * 40),
                )
        elif args.hook_service and args.hook_source_sha:
            if not args.hook_app_url:
                version_ref, _, app_url = resolve_app_hook_coordinates(
                    args.hook_service,
                    repo_root=repo_root,
                    after=args.after,
                    target_env=target_env,
                    domain=args.domain,
                    source_sha=args.hook_source_sha,
                )
            manifest_or_path = fetch_hooks_manifest(
                args.hook_service, args.hook_source_sha
            )
            hook_cwd, tmp_checkout = resolve_app_hook_cwd(
                args.hook_service,
                args.hook_source_sha,
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

    # Validate the durable desired-state coordinate before any production side
    # effect.  A branch, SHA, or pre-release must never deploy successfully and
    # only then fail while trying to record its marker.
    effective_before = args.before
    if promote_prod:
        current_production = assert_production_marker_advances(args.after, repo_root)
        if current_production:
            if effective_before and effective_before != current_production:
                raise SystemExit(
                    "::error::production before must match current marker release "
                    f"{current_production}; got {effective_before}"
                )
            effective_before = current_production
        elif not effective_before:
            raise SystemExit(
                "::error::first production marker requires an explicit known-production "
                "--before release"
            )
    # Fail-closed BEFORE planning/deploying: an apply run must promote a tag that is
    # reachable from reviewed main (Infra-011). dry-run is plan-only, so it may inspect
    # any ref (e.g. preview a not-yet-merged tag).
    if not args.dry_run:
        assert_after_on_main(args.after, repo_root)
    changed_files = (
        list(args.changed_file)
        if args.changed_file
        else changed_files_from_git(repo_root, effective_before or None, args.after)
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
        before=effective_before or None,
        after=args.after,
        app_release_tags=tag_overrides,
    )
    commands = build_deploy_commands(
        plan,
        iac_ref=args.after,
        domain=args.domain,
        timeout=args.timeout,
    )
    applied = commands_to_apply(
        commands,
        dry_run=args.dry_run,
        promote_prod=promote_prod,
        target_env=target_env,
    )
    deferred = [command for command in commands if command not in applied]
    results: list[dict] = []
    if applied:
        results = run_deploy_commands(applied)
    hook_results: list[dict[str, object]] = []
    if (
        args.run_post_deploy_hooks
        and applied
        and not args.dry_run
        and not any(result.get("returncode") != 0 for result in results)
    ):
        hook_results = run_post_deploy_app_hooks(
            plan,
            applied,
            repo_root=repo_root,
            after=args.after,
            target_env=target_env,
            domain=args.domain,
            slot=args.post_deploy_hook_slot,
        )
    hooks_failed = any(item.get("status") != "pass" for item in hook_results)
    production_marker = ""
    if (
        promote_prod
        and not args.dry_run
        and not any(result.get("returncode") != 0 for result in results)
        and not hooks_failed
    ):
        production_marker = record_production_marker(args.after, repo_root)

    run_url = github_run_url(os.environ)
    payload = {
        "alert_text": failure_alert_text(
            after=args.after,
            results=results,
            run_url=run_url,
            promote_prod=promote_prod,
        ),
        "plan": plan.to_dict(),
        "commands": [command.to_dict() for command in commands],
        # what actually ran this invocation vs what is left for a deliberate next step
        # (prod promotion, or — in dry-run — everything is just a plan).
        "applied": [command.to_dict() for command in applied],
        "deferred": [command.to_dict() for command in deferred],
        "promote_prod": promote_prod,
        "target_env": target_env,
        "production_marker": production_marker,
        "results": results,
        "hook_results": hook_results,
        "dry_run": args.dry_run,
    }
    print(json.dumps(payload, sort_keys=True))
    if args.output_json:
        Path(args.output_json).write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    write_summary(plan=plan, commands=commands, results=results, dry_run=args.dry_run)
    return (
        1
        if (any(result.get("returncode") != 0 for result in results) or hooks_failed)
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
