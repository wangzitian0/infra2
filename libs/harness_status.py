"""Read-only checkout/pin/remote status for the multi-repository harness."""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class RepositoryStatus:
    repository_id: str
    path: str
    release_identity: str
    initialized: bool
    checkout_head: str | None = None
    branch: str | None = None
    parent_pin: str | None = None
    pin_matches: bool | None = None
    remote_ref: str | None = None
    remote_head: str | None = None
    ahead: int | None = None
    behind: int | None = None
    dirty_paths: int | None = None
    checkout_release: str | None = None
    error: str = ""

    @property
    def remote_current(self) -> bool:
        return self.ahead == 0 and self.behind == 0

    @property
    def current(self) -> bool:
        return (
            self.initialized
            and not self.error
            and self.remote_current
            and self.pin_matches is not False
            and self.dirty_paths == 0
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["remote_current"] = self.remote_current
        payload["current"] = self.current
        return payload


@dataclass(frozen=True)
class WorkspaceStatus:
    repositories: tuple[RepositoryStatus, ...]

    @property
    def ok(self) -> bool:
        return all(item.initialized and not item.error for item in self.repositories)

    @property
    def current(self) -> bool:
        return bool(self.repositories) and all(
            item.current for item in self.repositories
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "current": self.current,
            "repositories": [item.to_dict() for item in self.repositories],
        }


def _git(
    checkout: Path,
    *args: str,
    runner: Runner = subprocess.run,
) -> subprocess.CompletedProcess:
    return runner(
        ["git", "-C", str(checkout), *args],
        capture_output=True,
        text=True,
    )


def _value(
    checkout: Path,
    *args: str,
    runner: Runner = subprocess.run,
) -> str | None:
    completed = _git(checkout, *args, runner=runner)
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def _parent_pin(root: Path, relative: str, *, runner: Runner) -> str | None:
    value = _value(root, "ls-tree", "HEAD", "--", relative, runner=runner)
    if not value:
        return None
    metadata = value.split("\t", 1)[0].split()
    return metadata[2].lower() if len(metadata) >= 3 else None


def _remote_ref(checkout: Path, *, runner: Runner) -> str | None:
    upstream = _value(
        checkout,
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{upstream}",
        runner=runner,
    )
    if upstream:
        return upstream
    origin_head = _value(
        checkout,
        "symbolic-ref",
        "--quiet",
        "--short",
        "refs/remotes/origin/HEAD",
        runner=runner,
    )
    if origin_head:
        return origin_head
    for candidate in ("origin/main", "origin/master"):
        if _value(checkout, "rev-parse", "--verify", candidate, runner=runner):
            return candidate
    return None


def repository_status(
    root: Path,
    repository: dict[str, Any],
    *,
    fetch: bool = False,
    runner: Runner = subprocess.run,
) -> RepositoryStatus:
    relative = str(repository["path"])
    checkout = (root / relative).resolve()
    common = {
        "repository_id": str(repository["id"]),
        "path": relative,
        "release_identity": str(repository["release_identity"]),
    }
    if not checkout.is_dir():
        return RepositoryStatus(**common, initialized=False, error="checkout missing")
    inside = _value(checkout, "rev-parse", "--is-inside-work-tree", runner=runner)
    if inside != "true":
        return RepositoryStatus(
            **common, initialized=False, error="not an initialized git checkout"
        )

    errors: list[str] = []
    if fetch:
        fetched = _git(checkout, "fetch", "--prune", "--tags", "origin", runner=runner)
        if fetched.returncode != 0:
            errors.append(f"fetch failed: {fetched.stderr.strip() or 'git error'}")

    head = _value(checkout, "rev-parse", "HEAD^{commit}", runner=runner)
    branch = _value(
        checkout, "symbolic-ref", "--quiet", "--short", "HEAD", runner=runner
    )
    remote_ref = _remote_ref(checkout, runner=runner)
    remote_head = (
        _value(checkout, "rev-parse", f"{remote_ref}^{{commit}}", runner=runner)
        if remote_ref
        else None
    )
    ahead: int | None = None
    behind: int | None = None
    if head and remote_ref and remote_head:
        relation = _value(
            checkout,
            "rev-list",
            "--left-right",
            "--count",
            f"HEAD...{remote_ref}",
            runner=runner,
        )
        if relation:
            try:
                ahead, behind = (int(value) for value in relation.split())
            except ValueError:
                errors.append(f"invalid ahead/behind result: {relation}")
        else:
            errors.append("ahead/behind relation is unresolved")
    if not head:
        errors.append("HEAD is unresolved")
    if not remote_ref or not remote_head:
        errors.append("remote head is unresolved")

    status_result = _git(checkout, "status", "--porcelain", runner=runner)
    dirty_paths: int | None = None
    if status_result.returncode == 0:
        dirty_paths = len(status_result.stdout.strip().splitlines())
    else:
        errors.append(
            f"working-tree status failed: {status_result.stderr.strip() or 'git error'}"
        )
    checkout_release = _value(checkout, "describe", "--tags", "--always", runner=runner)
    if checkout_release is None:
        errors.append("checkout release identity is unresolved")
    parent_pin = None
    pin_matches = None
    if repository.get("checkout") == "submodule":
        parent_pin = _parent_pin(root, relative, runner=runner)
        pin_matches = bool(parent_pin and head and parent_pin == head.lower())
        if parent_pin is None:
            errors.append("parent submodule pin is unresolved")

    return RepositoryStatus(
        **common,
        initialized=True,
        checkout_head=head.lower() if head else None,
        branch=branch or "(detached)",
        parent_pin=parent_pin,
        pin_matches=pin_matches,
        remote_ref=remote_ref,
        remote_head=remote_head.lower() if remote_head else None,
        ahead=ahead,
        behind=behind,
        dirty_paths=dirty_paths,
        checkout_release=checkout_release,
        error="; ".join(errors),
    )


def workspace_status(
    root: Path,
    manifest: dict[str, Any],
    *,
    fetch: bool = False,
    runner: Runner = subprocess.run,
) -> WorkspaceStatus:
    repositories = manifest.get("repositories")
    if not isinstance(repositories, list):
        return WorkspaceStatus(())
    return WorkspaceStatus(
        tuple(
            repository_status(root, repository, fetch=fetch, runner=runner)
            for repository in repositories
            if isinstance(repository, dict)
        )
    )
