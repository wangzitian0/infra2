"""Read-only checkout/pin/remote status for the multi-repository harness."""

from __future__ import annotations

import http.client
import json
import subprocess
import urllib.request
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from libs.core.harness.manifest import HEALTH_ENVIRONMENTS

Runner = Callable[..., subprocess.CompletedProcess]
# fetcher(url, timeout) returns the decoded JSON body. It raises OSError when nothing
# answers and ValueError when the body is not JSON. `fetch_json` is the real one.
Fetcher = Callable[[str, float], Any]
HEALTH_TIMEOUT_SECONDS = 8.0
MAX_BODY_BYTES = 1024 * 1024
HEALTH_USER_AGENT = (
    "Mozilla/5.0 (compatible; infra2-harness-status/1.0; "
    "+https://github.com/wangzitian0/infra2)"
)


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
    contract: str = "pinned"
    error: str = ""
    # Set by `observe_online` only: {"staging", "production", "note"} and the lock tag.
    served: dict[str, str | None] | None = None
    contract_lock: str | None = None

    @property
    def remote_current(self) -> bool:
        return self.ahead == 0 and self.behind == 0

    @property
    def current(self) -> bool:
        pin_satisfied = (
            True if self.contract == "snapshot" else self.pin_matches is not False
        )
        return (
            self.initialized
            and not self.error
            and self.remote_current
            and pin_satisfied
            and self.dirty_paths == 0
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["remote_current"] = self.remote_current
        payload["current"] = self.current
        if self.served is None:  # offline output keeps its original keys
            del payload["served"], payload["contract_lock"]
        return payload


@dataclass(frozen=True)
class WorkspaceStatus:
    repositories: tuple[RepositoryStatus, ...]
    observed_at: str | None = None  # UTC time of the `--online` reads

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
            **({"observed_at": self.observed_at} if self.observed_at else {}),
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
    role = repository.get("role")
    contract = str(
        repository.get("contract")
        or ("snapshot" if role == "external-application" else "pinned")
    )
    common = {
        "repository_id": str(repository["id"]),
        "path": relative,
        "release_identity": str(repository["release_identity"]),
        "contract": contract,
    }
    if not checkout.is_dir():
        return RepositoryStatus(**common, initialized=False, error="checkout missing")
    inside = _value(checkout, "rev-parse", "--is-inside-work-tree", runner=runner)
    if inside != "true":
        return RepositoryStatus(
            **common, initialized=False, error="not an initialized git checkout"
        )
    top_level = _value(checkout, "rev-parse", "--show-toplevel", runner=runner)
    if not top_level or Path(top_level).resolve() != checkout:
        return RepositoryStatus(
            **common,
            initialized=False,
            error=(
                f"path is not a repository root (Git top-level: {top_level or 'unknown'}); "
                "initialize this checkout with git submodule update --init if it is a submodule"
            ),
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


def fetch_json(url: str, timeout: float = HEALTH_TIMEOUT_SECONDS) -> Any:
    """GET an https URL and decode the JSON body. The only network call of `status`."""
    if not url.startswith("https://"):
        raise ValueError(f"not an https URL: {url!r}")
    request = urllib.request.Request(url, headers={"User-Agent": HEALTH_USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if not response.geturl().startswith("https://"):  # urllib follows redirects
            raise ValueError("redirected to a URL that is not https")
        body = response.read(MAX_BODY_BYTES + 1)
    if len(body) > MAX_BODY_BYTES:
        raise ValueError("response body is larger than 1 MiB")
    return json.loads(body)


def served_release(url: str, fetcher: Fetcher) -> tuple[str, str]:
    """(release, note) of one health URL: `version`, else `git_sha`. `unreachable`: no
    answer. `unknown`: an answer without a usable release."""
    try:
        body = fetcher(url, HEALTH_TIMEOUT_SECONDS)
    except (OSError, http.client.HTTPException) as exc:
        return "unreachable", type(exc).__name__
    except (ValueError, RecursionError) as exc:  # the body is not usable JSON
        return "unknown", type(exc).__name__
    for field in ("version", "git_sha"):
        value = body.get(field) if isinstance(body, dict) else None
        if isinstance(value, str) and value.strip():
            value = value.strip()
            return (
                (value[:80], "")
                if value.isprintable()
                else ("unknown", f"unprintable {field}")
            )
    return "unknown", "no version or git_sha"


def _served(
    entry: dict[str, Any],
    root: Path,
    fetcher: Fetcher,
    marker_status: Callable[..., Any] | None,
) -> dict[str, str | None]:
    health = entry.get("health")
    if isinstance(health, dict):  # an App: read its health URLs
        row: dict[str, str | None] = {"staging": None, "production": None}
        notes = []
        for environment in HEALTH_ENVIRONMENTS:
            if environment in health:
                row[environment], note = served_release(health[environment], fetcher)
                if note:
                    notes.append(f"{environment}: {note}")
        return {**row, "note": "; ".join(notes)}
    if entry.get("checkout") != "root":
        return {"staging": None, "production": None, "note": ""}
    from libs.deploy.release_markers import marker_status as read_markers  # stdlib only

    try:
        marker = (marker_status or read_markers)(repo_root=root)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        note = f"markers: {type(exc).__name__}: {exc}"[:100]
        return {"staging": "unknown", "production": "unknown", "note": note}
    behind = marker.releases_behind
    lag = (
        f"{behind} release{'s' if behind != 1 else ''} behind" if behind else "current"
    )
    return {
        "staging": marker.newest_tag,
        "production": marker.production_marker,
        "note": f"marker {lag}",
    }


def _contract_lock(root: Path, repository_path: str) -> str | None:
    """Tag, else the first 8 characters of the commit, of `apps.<directory>` in the lock."""
    lock = root / "contracts" / "apps.lock.json"
    if not lock.is_file():
        return None
    try:
        app = json.loads(lock.read_text(encoding="utf-8"))["apps"].get(
            Path(repository_path).name
        )
        if app is None:
            return None
        value = app["tag"] or app["commit"][:8]
        return value if isinstance(value, str) and value.isprintable() else "unknown"
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return "unknown"


def observe_online(
    status: WorkspaceStatus,
    root: Path,
    manifest: dict[str, Any],
    *,
    fetcher: Fetcher,
    marker_status: Callable[..., Any] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> WorkspaceStatus:
    """`status` plus what each repository serves online and its contract lock.

    The network is read only through `fetcher`. infra2 is read from local git tags.
    """
    entries = {entry["id"]: entry for entry in manifest["repositories"]}
    items = []
    for item in status.repositories:
        served = _served(entries[item.repository_id], root, fetcher, marker_status)
        lock = _contract_lock(root, item.path)
        items.append(replace(item, served=served, contract_lock=lock))
    observed_at = now().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return WorkspaceStatus(tuple(items), observed_at)


def render_online_table(status: WorkspaceStatus) -> list[str]:
    """The online table as text lines. Empty when `status` has no online reads."""
    if status.observed_at is None:
        return []
    rows = [("repo", "pin", "checkout", "contract(lock)", "staging", "production")]
    notes = [""]
    for item in status.repositories:
        served = item.served or {}
        rows.append(
            (
                item.repository_id,
                (item.parent_pin or "-")[:8],
                (item.checkout_head or "-")[:8],
                item.contract_lock or "-",
                served.get("staging") or "-",
                served.get("production") or "-",
            )
        )
        notes.append(f"({served['note']})" if served.get("note") else "")
    widths = [max(map(len, column)) for column in zip(*rows)]
    lines = [f"online (observed {status.observed_at}; infra2 row from local git tags):"]
    for row, note in zip(rows, notes):
        cells = "  ".join(cell.ljust(width) for cell, width in zip(row, widths))
        lines.append(f"{cells}  {note}".rstrip())
    return lines


__all__ = [
    "RepositoryStatus",
    "Runner",
    "WorkspaceStatus",
    "fetch_json",
    "observe_online",
    "render_online_table",
    "repository_status",
    "served_release",
    "workspace_status",
]
