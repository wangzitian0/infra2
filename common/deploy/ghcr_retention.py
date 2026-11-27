"""Delete old GHCR image versions that no release, no recent build and no kept image needs.

A version is a root when one of these is true:

1. a tag is a release tag `vX.Y.Z` or `latest`;
2. GHCR created it in the last `KEEP_DAYS` days;
3. a tag names a commit that reached the App's `main` in the last `KEEP_DAYS` days. A cached
   build pushes the same digest again: GHCR adds the new tag to the old version and keeps
   its old `created_at`.

The plan keeps every root and every manifest that a kept index names (platform images,
attestations). It deletes every other version. Without `--apply` the tool deletes nothing.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx
from infra2_sdk.service import (
    HttpClientSettings,
    create_http_client,
    request_with_retry,
)

OWNER = "wangzitian0"
# The App packages that publish one version set per commit. Small packages
# (infra2-canary-todo, infra2-s3) are not listed: each version there is a release.
PACKAGE_REPOS = {
    "truealpha-app-web": "truealpha",
    "truealpha-data-engine": "truealpha",
    "truealpha-llm-service": "truealpha",
    "finance_report-backend": "finance_report",
    "finance_report-frontend": "finance_report",
}
PACKAGES = tuple(PACKAGE_REPOS)
KEEP_DAYS = 30
# Several sessions and the release train share one GitHub login (5,000 REST calls per
# hour). A deletion run stops when fewer calls than this are left; the next run resumes.
RESERVED_CALLS = 2000
# A deletion batch is planned from a fresh list, so a tag added during the run reaches
# the plan within one batch.
BATCH = 500
SHORT_SHA = re.compile(r"^(?:sha-)?([0-9a-f]{7})$")
RELEASE_TAG = re.compile(r"^v\d+\.\d+\.\d+$")
INDEX_TYPES = frozenset(
    {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
)
MANIFEST_ACCEPT = ",".join(
    sorted(INDEX_TYPES)
    + [
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


@dataclass(frozen=True)
class Version:
    id: int
    digest: str
    created_at: datetime
    tags: tuple[str, ...] = ()


@dataclass
class Plan:
    package: str
    keep: list[Version] = field(default_factory=list)
    delete: list[Version] = field(default_factory=list)


def is_root(
    version: Version,
    now: datetime,
    keep_days: int = KEEP_DAYS,
    recent_commits: frozenset[str] = frozenset(),
) -> bool:
    for tag in version.tags:
        if RELEASE_TAG.match(tag) or tag == "latest":
            return True
        short = SHORT_SHA.match(tag)
        if short and short.group(1) in recent_commits:
            return True
    return version.created_at >= now - timedelta(days=keep_days)


def plan_package(
    package: str,
    versions: Iterable[Version],
    children_of: Callable[[str], Iterable[str]],
    now: datetime,
    keep_days: int = KEEP_DAYS,
    recent_commits: frozenset[str] = frozenset(),
) -> Plan:
    """Split versions into keep and delete. `children_of(digest)` names an index's manifests."""
    versions = list(versions)
    keep_digests: set[str] = set()
    pending = [v.digest for v in versions if is_root(v, now, keep_days, recent_commits)]
    while pending:
        digest = pending.pop()
        if digest in keep_digests:
            continue
        keep_digests.add(digest)
        pending.extend(children_of(digest))
    plan = Plan(package)
    for version in versions:
        (plan.keep if version.digest in keep_digests else plan.delete).append(version)
    return plan


def parse_versions(rows: Iterable[Mapping]) -> list[Version]:
    return [
        Version(
            id=int(row["id"]),
            digest=str(row["name"]),
            created_at=datetime.fromisoformat(
                str(row["created_at"]).replace("Z", "+00:00")
            ),
            tags=tuple(row.get("metadata", {}).get("container", {}).get("tags", [])),
        )
        for row in rows
    ]


def children_in(manifest: Mapping) -> list[str]:
    if manifest.get("mediaType") not in INDEX_TYPES and "manifests" not in manifest:
        return []
    return [str(entry["digest"]) for entry in manifest.get("manifests", [])]


class ManifestGone(LookupError):
    """The registry has no manifest for a digest that the package list named."""


class Ghcr:
    """GitHub Packages API (list, delete) and the GHCR registry API (read manifests)."""

    def __init__(
        self,
        token: str,
        owner: str = OWNER,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.owner = owner
        self.sleep = sleep
        self.client = client or create_http_client(
            HttpClientSettings(timeout_seconds=30.0), propagate_trace=False
        )
        self.api_headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        self._registry_tokens: dict[str, str] = {}

    def _get(self, url: str, **kwargs) -> httpx.Response:
        """GET with 3 attempts on a timeout, a connection error or a 5xx. A read changes nothing."""
        return request_with_retry(
            self.client,
            "GET",
            url,
            max_attempts=3,
            retry_statuses=(500, 502, 503, 504),
            backoff_base=0.25,
            sleep=self.sleep,
            **kwargs,
        )

    def versions(self, package: str, attempts: int = 3) -> list[Version]:
        """List twice and require the same ids. A deletion during a paged list skips a version."""
        for _ in range(attempts):
            first = self._list(package)
            second = self._list(package)
            if {v.id for v in first} == {v.id for v in second}:
                return second
        raise RuntimeError(
            f"{package}: two version lists differ {attempts} times; stop"
        )

    def remaining_calls(self) -> int:
        reply = self._get("https://api.github.com/rate_limit", headers=self.api_headers)
        reply.raise_for_status()
        return int(reply.json()["resources"]["core"]["remaining"])

    def _list(self, package: str) -> list[Version]:
        rows: list[Mapping] = []
        url = f"https://api.github.com/users/{self.owner}/packages/container/{package}/versions"
        params: dict[str, int] = {"per_page": 100, "page": 1}
        while True:
            reply = self._get(url, headers=self.api_headers, params=params)
            reply.raise_for_status()
            page = reply.json()
            rows.extend(page)
            if len(page) < 100:
                return parse_versions(rows)
            params["page"] += 1

    def _registry_token(self, package: str) -> str:
        if package not in self._registry_tokens:
            reply = self._get(
                "https://ghcr.io/token",
                params={
                    "service": "ghcr.io",
                    "scope": f"repository:{self.owner}/{package}:pull",
                },
            )
            reply.raise_for_status()
            self._registry_tokens[package] = reply.json()["token"]
        return self._registry_tokens[package]

    def children(self, package: str, digest: str) -> list[str]:
        reply = self._get(
            f"https://ghcr.io/v2/{self.owner}/{package}/manifests/{digest}",
            headers={
                "Authorization": f"Bearer {self._registry_token(package)}",
                "Accept": MANIFEST_ACCEPT,
            },
        )
        if reply.status_code == 404:
            raise ManifestGone(digest)
        # Fail closed: a kept index that cannot be read must stop the plan, or its
        # children would be deleted.
        reply.raise_for_status()
        return children_in(reply.json())

    def delete(self, package: str, version_id: int, attempts: int = 3) -> int:
        """Delete one version. Return the REST calls left in this hour for the shared login.

        A timeout, a connection error or a 5xx gets 3 attempts, 10 s and 20 s apart. The first attempt can delete
        the version before its reply is lost, so a 404 after a failed attempt is a success."""
        url = f"https://api.github.com/users/{self.owner}/packages/container/{package}/versions/{version_id}"
        for attempt in range(attempts):
            last = attempt == attempts - 1
            if attempt:
                self.sleep(10 * attempt)
            try:
                reply = self.client.delete(url, headers=self.api_headers)
            except (httpx.TimeoutException, httpx.TransportError):
                if last:
                    raise
                continue
            if reply.status_code == 404 and attempt > 0:
                return int(reply.headers.get("x-ratelimit-remaining", "0"))
            if reply.status_code >= 500 and not last:
                continue
            reply.raise_for_status()
            return int(reply.headers.get("x-ratelimit-remaining", "0"))
        raise AssertionError("unreachable")


def clone(
    runner: Callable[..., subprocess.CompletedProcess],
    argv: list[str],
    env: Mapping[str, str],
    sleep: Callable[[float], None],
    attempts: int = 3,
) -> None:
    """Run `git clone` with 3 attempts 30 s apart. A network error after a sleep is common."""
    for attempt in range(attempts):
        try:
            runner(
                argv,
                check=True,
                capture_output=True,
                text=True,
                timeout=300,
                env=env,
            )
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if attempt == attempts - 1:
                raise
            sleep(30)
            # A failed clone can leave files in the target; git refuses a non-empty target.
            target = argv[-1]
            shutil.rmtree(target, ignore_errors=True)
            os.makedirs(target, exist_ok=True)


def recent_commits(
    repo: str,
    since: datetime,
    owner: str = OWNER,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
) -> frozenset[str]:
    """Short SHAs of the commits on `main` of `repo` since `since`. Uses git, not the REST API.

    Git filters by committer date. A caller passes a window wider than `KEEP_DAYS`, so an old
    commit that reached `main` late is still in the set."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    with tempfile.TemporaryDirectory() as tmp:
        url = f"https://github.com/{owner}/{repo}.git"
        stamp = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        clone(
            runner,
            [
                "git",
                "clone",
                "--quiet",
                "--bare",
                "--filter=blob:none",
                "--single-branch",
                "--branch",
                "main",
                f"--shallow-since={stamp}",
                url,
                tmp,
            ],
            env,
            sleep,
        )
        log = runner(
            ["git", "-C", tmp, "log", "--format=%H", f"--since={stamp}", "main"],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    shas = frozenset(line[:7] for line in log.stdout.split() if line)
    if not shas:
        raise RuntimeError(
            f"{repo}: no main commit since {stamp}; refuse to plan without them"
        )
    return shas


def build_plan(
    ghcr: Ghcr,
    package: str,
    now: datetime,
    keep_days: int = KEEP_DAYS,
    recent: frozenset[str] = frozenset(),
) -> Plan:
    versions = ghcr.versions(package)
    gone: set[str] = set()

    def read(digest: str) -> list[str] | None:
        try:
            return ghcr.children(package, digest)
        except ManifestGone:
            gone.add(digest)
            return None

    roots = [v.digest for v in versions if is_root(v, now, keep_days, recent)]
    with ThreadPoolExecutor(8) as pool:
        cache = dict(zip(roots, pool.map(read, roots)))

    def children_of(digest: str) -> list[str]:
        if digest not in cache:
            cache[digest] = read(digest)
        return cache[digest] or []

    plan = plan_package(package, versions, children_of, now, keep_days, recent)
    if gone:
        # A push can replace a version between the list and the read. Accept a missing
        # manifest only when a fresh list also lacks it; else stop before any deletion.
        still_listed = gone & {v.digest for v in ghcr.versions(package)}
        if still_listed:
            raise RuntimeError(
                f"{package}: listed versions have no manifest: {sorted(still_listed)}"
            )
        plan.delete = [v for v in plan.delete if v.digest not in gone]
        plan.keep = [v for v in plan.keep if v.digest not in gone]
    return plan


def apply_plan(
    plan: Plan,
    delete: Callable[[str, int], int],
    remaining: int,
    reserved_calls: int = RESERVED_CALLS,
    batch: int = BATCH,
) -> tuple[int, int]:
    """Delete up to `batch` planned versions, oldest first, above the call reserve.

    Return (deleted, calls left)."""
    deleted = 0
    for version in sorted(plan.delete, key=lambda v: v.created_at)[:batch]:
        if remaining < reserved_calls:
            break
        remaining = delete(plan.package, version.id)
        deleted += 1
    return deleted, remaining


def apply_package(
    plan_for: Callable[[], Plan],
    delete: Callable[[str, int], int],
    remaining_calls: Callable[[], int],
    reserved_calls: int = RESERVED_CALLS,
    batch: int = BATCH,
) -> tuple[int, bool]:
    """Plan from a fresh list, delete one batch, repeat. Return (deleted, finished).

    The call count is read again after each plan, so the list calls of a plan count too."""
    total = 0
    while True:
        plan = plan_for()
        if not plan.delete:
            return total, True
        remaining = remaining_calls()
        if remaining < reserved_calls:
            return total, False
        deleted, _ = apply_plan(plan, delete, remaining, reserved_calls, batch)
        total += deleted


def token_from_env() -> str:
    token = os.environ.get("GHCR_RETENTION_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not token:
        raise SystemExit(
            "set GHCR_RETENTION_TOKEN or GH_TOKEN (scope read:packages; delete:packages for --apply)"
        )
    return token
