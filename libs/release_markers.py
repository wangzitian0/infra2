"""infra2's two release coordinates, read from git alone.

`newest_release_tag` is what a staging deploy runs at; `production_marker` is what
production is pinned to, and `marker_status` is the gap between them — the daily #650
report.

Separate from libs/app_deploy_request because this is pure git: no deploy request, no
secret store, no SDK. The report runs in an ops-checks job that installs none of that, and
importing it through the request adapter killed the job on `ModuleNotFoundError: No module
named 'infra2_sdk'` (run 34430724977, 2026-09-10).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_SEMVER_TAG_RE = re.compile(r"\Av[0-9]+\.[0-9]+\.[0-9]+\Z")
_PRODUCTION_MARKER_PREFIX = "production/"
# A production release also promotes the platform leg — truealpha/app carries
# truealpha/data_engine — from the marker's checkout, and only v1.1.59 and later resolve
# that image from the release's own DEPLOY_VERSION_REF (#632). Run against an older
# marker, the deployer recreates production from whatever digest an operator last wrote
# to Vault, and the release learns about it only in its health confirmation, after
# production has already been recreated: that is how v0.0.48 went out on 2026-09-08 from
# a marker last moved on 2026-07-30 (#650). Refuse the release instead, and say what to
# promote.
MINIMUM_PRODUCTION_MARKER = "v1.1.59"


def _merged_release_tags(
    pattern: str, *, repo_root: str | Path, runner=subprocess.run
) -> list[str]:
    result = runner(
        [
            "git",
            "tag",
            "--list",
            pattern,
            "--merged",
            "HEAD",
            "--sort=-version:refname",
        ],
        cwd=Path(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _version_key(tag: str) -> tuple[int, ...]:
    return tuple(int(part) for part in tag.removeprefix("v").split("."))


def newest_release_tag(*, repo_root: str | Path, runner=subprocess.run) -> str:
    """The newest infra2 release merged into HEAD — the ref a staging deploy runs at."""
    for tag in _merged_release_tags("v*.*.*", repo_root=repo_root, runner=runner):
        if _SEMVER_TAG_RE.fullmatch(tag):
            return tag
    raise ValueError("no released infra2 vX.Y.Z tag is merged into HEAD")


def production_marker(*, repo_root: str | Path, runner=subprocess.run) -> str:
    """The release production is pinned to — the ref a production deploy runs at."""
    pattern = f"{_PRODUCTION_MARKER_PREFIX}v*.*.*"
    for tag in _merged_release_tags(pattern, repo_root=repo_root, runner=runner):
        cleaned = tag.removeprefix(_PRODUCTION_MARKER_PREFIX)
        if _SEMVER_TAG_RE.fullmatch(cleaned):
            return cleaned
        raise ValueError(f"invalid production marker {tag!r}")
    raise ValueError(
        "no production/vX.Y.Z marker is merged into HEAD; production IaC state is unknown"
    )


@dataclass(frozen=True)
class MarkerStatus:
    """Where production is pinned, next to the newest release infra2 has cut."""

    production_marker: str
    newest_tag: str
    releases_behind: int

    @property
    def stale(self) -> bool:
        """Whether a production release run at this marker would use a deployer that
        cannot pin the data engine from the release itself (#632)."""
        return _version_key(self.production_marker) < _version_key(
            MINIMUM_PRODUCTION_MARKER
        )

    def line(self) -> str:
        lag = (
            "current"
            if not self.releases_behind
            else f"{self.releases_behind} release(s) behind"
        )
        verdict = (
            f" — STALE: predates {MINIMUM_PRODUCTION_MARKER}, the release pin (#632); "
            "promote infra2 to production before the next app release (#650)"
            if self.stale
            else ""
        )
        return (
            f"production marker {self.production_marker} ({lag}); "
            f"newest release {self.newest_tag}{verdict}"
        )


def marker_status(*, repo_root: str | Path, runner=subprocess.run) -> MarkerStatus:
    """Both coordinates and the gap between them — the daily #650 lag report."""
    marker = production_marker(repo_root=repo_root, runner=runner)
    releases = [
        tag
        for tag in _merged_release_tags("v*.*.*", repo_root=repo_root, runner=runner)
        if _SEMVER_TAG_RE.fullmatch(tag)
    ]
    if not releases:
        raise ValueError("no released infra2 vX.Y.Z tag is merged into HEAD")
    marker_key = _version_key(marker)
    behind = sum(1 for tag in releases if _version_key(tag) > marker_key)
    return MarkerStatus(marker, releases[0], behind)
