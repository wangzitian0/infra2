#!/usr/bin/env python3
"""Check and refresh infra2's copies of the App environment manifests.

A deploy reads only the copies in ``contracts/``. ``contracts/apps.lock.json`` records
the commit of each App and the sha256 of each copy. This file uses the standard library only.

    uv run python tools/fetch_app_manifests.py                 # offline check (CI)
    uv run python tools/fetch_app_manifests.py --verify        # check, then compare with upstream (network)
    uv run python tools/fetch_app_manifests.py --sync APP REF  # write copies and lock
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
LOCK = "contracts/apps.lock.json"


def _lock(root: Path) -> dict:
    return json.loads((root / LOCK).read_text(encoding="utf-8"))


def _inside(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return len(parts) > 1 and parts[0] == "contracts" and ".." not in parts


def origins(root: Path = ROOT) -> dict[str, tuple[str, str]]:
    """Map each copy path to its ``(app, upstream path)``, as the lock records it."""
    return {f["path"]: (f["app"], f["upstream"]) for f in _lock(root)["files"]}


def check(root: Path = ROOT, fetch=None, ls_remote=None) -> list[str]:
    """One line per problem. An empty list means the copies match the lock.

    With ``fetch``, a clean check also compares every file with the upstream bytes at the locked commit.
    """
    problems = []
    try:
        lock = _lock(root)
        for name, app in lock["apps"].items():
            if not isinstance(app.get("commit"), str) or not re.fullmatch(
                r"[0-9a-f]{40}", app["commit"]
            ):
                problems.append(f"{LOCK}: apps.{name}.commit is not 40 lowercase hex")
            repository = app.get("repository")
            if not isinstance(repository, str) or not re.fullmatch(
                r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
            ):
                problems.append(f"{LOCK}: apps.{name}.repository is not owner/name")
            tag = app.get("tag")
            if tag is not None and (
                not isinstance(tag, str)
                or not tag.strip()
                or tag != tag.strip()
                or not tag.isprintable()
            ):
                problems.append(
                    f"{LOCK}: apps.{name}.tag must be null or a non-empty printable string"
                )
        for f in lock["files"]:
            path, sha256, up = f["path"], f["sha256"], PurePosixPath(f["upstream"])
            if f["app"] not in lock["apps"]:
                problems.append(f"{path}: app {f['app']!r} is not in apps")
            if up.is_absolute() or ".." in up.parts or not up.parts:
                problems.append(f"{path}: upstream is not a relative path without ..")
            if not _inside(path):
                problems.append(f"{path}: outside contracts/")
            elif not (root / path).is_file():
                problems.append(f"{path}: missing file")
            elif not re.fullmatch(r"[0-9a-f]{64}", sha256):
                problems.append(f"{path}: sha256 is not 64 lowercase hex")
            elif hashlib.sha256((root / path).read_bytes()).hexdigest() != sha256:
                problems.append(f"{path}: sha256 differs from the lock")
        listed = {f["path"] for f in lock["files"]} | {LOCK}
        for p in [root / "contracts", *sorted((root / "contracts").rglob("*"))]:
            rel = p.relative_to(root).as_posix()
            if p.is_symlink():
                problems.append(f"{rel}: symlink")
            elif p.suffix == ".json" and rel not in listed:
                problems.append(f"{rel}: not listed in {LOCK}")
        if fetch and not problems:  # the copies equal the lock: now ask upstream
            if ls_remote:
                for name, app in lock["apps"].items():
                    repo_url = f"https://github.com/{app['repository']}.git"
                    tag = app.get("tag")
                    if tag:
                        tag_sha, _ = resolve(app["repository"], tag, ls_remote)
                        if tag_sha != app["commit"]:
                            problems.append(
                                f"{LOCK}: apps.{name}.tag {tag!r} resolves to {tag_sha[:12]}, not {app['commit'][:12]}"
                            )
                    else:
                        main_lines = ls_remote(repo_url, "refs/heads/main").splitlines()
                        if not main_lines:
                            problems.append(
                                f"{LOCK}: apps.{name}.repository {app['repository']} has no refs/heads/main"
                            )
            for f in lock["files"]:
                app = lock["apps"][f["app"]]
                url = f"https://raw.githubusercontent.com/{app['repository']}/{app['commit']}/{f['upstream']}"
                body = fetch(url)
                if not body or hashlib.sha256(body).hexdigest() != f["sha256"]:
                    problems.append(f"{f['path']}: upstream empty or differs ({url})")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        problems.append(f"{LOCK}: unreadable or malformed ({exc!r})")
    return problems


def http_get(url, retries=2, sleep=time.sleep, opener=urllib.request.urlopen) -> bytes:
    """GET ``url``. Retry a connection error, 5xx, 408, 429 (#824). Other errors fail at once."""
    for attempt in range(retries + 1):
        try:
            with opener(url, timeout=30) as response:
                return response.read()
        except (OSError, http.client.HTTPException) as exc:
            code = exc.code if isinstance(exc, urllib.error.HTTPError) else 0
            cert = isinstance(getattr(exc, "reason", exc), ssl.SSLCertVerificationError)
            permanent = cert or (400 <= code < 500 and code not in (408, 429))
            if attempt == retries or permanent:
                raise SystemExit(f"fetching {url} failed: {exc}") from exc
            sleep(2 ** (attempt + 1))


def git_ls_remote(url: str, ref: str) -> str:
    """The output of ``git ls-remote URL REF REF^{}``."""
    cmd = ["git", "ls-remote", url, ref, f"{ref}^{{}}"]
    run = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if run.returncode:
        raise SystemExit(f"git ls-remote {url} failed: {run.stderr.strip()}")
    return run.stdout


def resolve(repository: str, ref: str, ls_remote=git_ls_remote):
    """``(commit, tag)`` of a full sha, a tag or a branch. A peeled ``^{}`` line wins."""
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        return ref, None
    lines = ls_remote(f"https://github.com/{repository}.git", ref).splitlines()
    rows = {name: sha for sha, name in (line.split("\t") for line in lines)}
    for name in (f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}", f"refs/heads/{ref}"):
        if name in rows:
            return rows[name], ref if name.startswith("refs/tags/") else None
    raise SystemExit(f"{repository} has no tag or branch {ref!r}")


def sync(app, ref, root=ROOT, ls_remote=git_ls_remote, fetch=http_get) -> None:
    """Write the copies of ``app`` at ``ref`` and refresh the lock.
    Every file downloads before the first write, so a failure changes nothing."""
    lock = _lock(root)
    if app not in lock["apps"]:
        raise SystemExit(f"unknown app {app!r}; known: {', '.join(lock['apps'])}")
    repository = lock["apps"][app]["repository"]
    commit, tag = resolve(repository, ref, ls_remote)
    entries = [f for f in lock["files"] if f["app"] == app]
    if not all(_inside(f["path"]) for f in entries):
        raise SystemExit(f"{LOCK}: a path of {app} is outside contracts/")
    base = f"https://raw.githubusercontent.com/{repository}/{commit}/"
    bodies = [fetch(base + f["upstream"]) for f in entries]
    if not all(bodies):
        raise SystemExit(f"{app}@{commit[:12]}: an upstream body is empty")
    for entry, body in zip(entries, bodies):
        (root / entry["path"]).parent.mkdir(parents=True, exist_ok=True)
        (root / entry["path"]).write_bytes(body)
        entry["sha256"] = hashlib.sha256(body).hexdigest()
        print(f"{entry['path']}: sha256 {entry['sha256'][:12]} ({app}@{commit[:12]})")
    lock["apps"][app] = {"commit": commit, "repository": repository, "tag": tag}
    lock["files"].sort(key=lambda f: f["path"])
    (root / LOCK).write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
    print("next: run tools/secrets_render.py --write, then --check")


def main(argv=None, root=ROOT, **network) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sync", nargs=2, metavar=("APP", "REF"))
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    if args.sync:
        sync(*args.sync, root=root, **network)
        if not args.verify:
            return 0
    errors = check(
        root,
        (network.get("fetch") or http_get) if args.verify else None,
        (network.get("ls_remote") or git_ls_remote) if args.verify else None,
    )
    print("\n".join(errors) or f"app manifest copies: ok ({len(origins(root))} files)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
