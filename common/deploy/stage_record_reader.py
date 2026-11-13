#!/usr/bin/env python3
"""List the stage records of this repository that the Actions artifacts hold (#1645).

A stage task uploads each record (``common/deploy/stage_record.py``) as the artifact
``stage-<env>-<phase>-<subject>``. ``list`` prints the records that pass the same provenance
checks as ``tools/ci_tree_reuse.py``. A record counts only when all of these are true:

1. The artifact is not expired.
2. The run that uploaded it is in this repository (not a fork).
3. The run is a run of the workflow that owns the env, with an event that the env allows.
4. The record names the same env and the same artifact name, and its ``run_url`` is that run.

The command line is ``python -m tools.stage_records list``.

Fail safe: an API error or any doubt about one artifact skips that artifact, and the message
goes to stderr. Standard library only: a step may run it before any dependency is installed.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

from common.deploy.github_api import Opener, get_json, github_headers

AGENT = "infra2-stage-records/1.0"
PREFIX = "stage-"
MAX_PAGES = 100
PHASES = ("pre", "operation", "post")
_RECONCILE = ".github/workflows/reconcile-iac-inputs.yml"
#: env -> (workflow path, events that may write its records)
WRITERS: dict[str, tuple[str, frozenset[str]]] = {
    "staging.release_validation": (
        _RECONCILE,
        frozenset({"push", "workflow_dispatch"}),
    ),
    "prod.release_integrity": (_RECONCILE, frozenset({"push", "workflow_dispatch"})),
    "github_ci.merge_authority": (
        ".github/workflows/infra-ci.yml",
        frozenset({"push"}),
    ),
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def open_url(request: urllib.request.Request, timeout: float = 30) -> Any:
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


def _download(url: str, token: str, opener: Opener) -> bytes:
    """The zip bytes. GitHub answers 302; the signed target takes no Authorization header."""
    try:
        request = urllib.request.Request(url, headers=github_headers(token, AGENT))
        with opener(request, timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as err:
        if err.code != 302 or not err.headers.get("Location"):
            raise
        with opener(
            urllib.request.Request(err.headers["Location"]), timeout=60
        ) as response:
            return response.read()


def _checked_record(
    artifact: dict, repo: str, token: str, opener: Opener
) -> dict | None:
    """The record of one artifact, or None when a provenance check fails."""
    origin = artifact.get("workflow_run")
    if artifact.get("expired") is not False or not isinstance(origin, dict):
        return None
    run_id, repo_id = origin.get("id"), origin.get("repository_id")
    if (
        not isinstance(run_id, int)
        or repo_id is None
        or repo_id != origin.get("head_repository_id")
    ):
        return None
    run = get_json(f"/repos/{repo}/actions/runs/{run_id}", token, opener, AGENT)
    archive = zipfile.ZipFile(
        io.BytesIO(_download(str(artifact["archive_download_url"]), token, opener))
    )
    files = [n for n in archive.namelist() if n.endswith(".json")]
    if len(files) != 1:
        return None
    record = json.loads(archive.read(files[0]).decode("utf-8"))
    if not isinstance(record, dict) or record.get("schema") != 1:
        return None
    env, phase, subject = (
        record.get("env"),
        record.get("phase"),
        str(record.get("subject")),
    )
    writer = WRITERS.get(str(env))
    if (
        writer is None
        or run.get("path") != writer[0]
        or run.get("event") not in writer[1]
        or phase not in PHASES
        or artifact.get("name") != f"{PREFIX}{env}-{phase}-{subject.replace(':', '-')}"
        or not str(record.get("run_url", "")).endswith(f"/actions/runs/{run_id}")
        or not isinstance(record.get("checks"), list)
        or not _run_is_the_subject(run, subject, repo, token, opener)
    ):
        return None
    return record


def _run_is_the_subject(
    run: dict, subject: str, repo: str, token: str, opener: Opener
) -> bool:
    """The run built the commit that the subject names, on main or on that tag.

    ``sha:<commit>``: a run on main whose ``head_sha`` is the commit. ``tag:<tag>`` from a tag
    push: a run on that tag whose ``head_sha`` is the commit of the tag. ``tag:<tag>`` from a
    dispatch: a run on main. A run of any other branch never counts, whatever it uploaded.
    """
    kind, _, ident = subject.partition(":")
    branch, head = run.get("head_branch"), run.get("head_sha")
    if kind == "sha":
        return branch == "main" and head == ident
    if kind != "tag":
        return False
    if run.get("event") == "push":
        commit = get_json(f"/repos/{repo}/commits/{ident}", token, opener, AGENT)
        return branch == ident and head == commit.get("sha")
    return branch == "main"


def _artifacts(repo: str, token: str, opener: Opener) -> Iterator[dict]:
    """Artifacts of the repository, newest first."""
    for page in range(1, MAX_PAGES + 1):
        query = urllib.parse.urlencode({"per_page": 100, "page": page})
        data = get_json(
            f"/repos/{repo}/actions/artifacts?{query}", token, opener, AGENT
        )
        items = [a for a in data.get("artifacts", []) if isinstance(a, dict)]
        yield from items
        if len(items) < 100:
            return


def _created(artifact: dict) -> datetime:
    return datetime.fromisoformat(str(artifact["created_at"]).replace("Z", "+00:00"))


def list_records(
    repo: str,
    token: str,
    *,
    env: str = "",
    subject: str = "",
    phase: str = "",
    last: int = 0,
    since: datetime | None = None,
    opener: Opener = open_url,
) -> list[dict]:
    """The records that pass the provenance checks, newest first.

    ``env`` is a stage id (``staging.release_validation``) or its first part (``staging``).
    ``last`` keeps the newest N subjects (0 keeps all). ``since`` ignores older artifacts.
    """
    prefix = PREFIX + env + ("-" if "." in env else ".") if env else PREFIX
    found: list[tuple[datetime, dict]] = []
    for artifact in _artifacts(repo, token, opener):
        name = str(artifact.get("name") or "")
        if since and _created(artifact) < since:
            break
        if not name.startswith(prefix) or (phase and f"-{phase}-" not in name):
            continue
        if subject and not name.endswith("-" + subject.replace(":", "-")):
            continue
        try:
            record = _checked_record(artifact, repo, token, opener)
        except Exception as exc:  # noqa: BLE001 - one bad artifact never hides the others
            print(
                f"stage_records: skip {name} ({type(exc).__name__}: {exc})",
                file=sys.stderr,
            )
            continue
        if (
            record
            and record["phase"] == (phase or record["phase"])
            and record["subject"] == (subject or record["subject"])
        ):
            found.append((_created(artifact), record))
            # Newest first: once N+1 subjects are seen, the N newest are complete.
            if last and len({(r["env"], r["subject"]) for _, r in found}) > last:
                break
    found.sort(key=lambda item: (item[0], PHASES.index(item[1]["phase"])), reverse=True)
    records = [record for _, record in found]
    if last:
        keep = list(dict.fromkeys((r["env"], r["subject"]) for r in records))[:last]
        records = [r for r in records if (r["env"], r["subject"]) in keep]
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser(
        "list", help="print the records that pass the provenance checks"
    )
    cmd.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    cmd.add_argument(
        "--env", default="", help="stage id or its first part, e.g. staging"
    )
    cmd.add_argument("--subject", default="", help="e.g. tag:v1.2.3")
    cmd.add_argument("--phase", choices=PHASES, default="")
    cmd.add_argument("--last", type=int, default=0, help="newest N subjects")
    cmd.add_argument("--since-days", type=float, default=0.0)
    cmd.add_argument(
        "--json", action="store_true", help="accepted; the output is always JSON"
    )
    args = parser.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN", "")
    if not (args.repo and token):
        print("stage_records: --repo and GITHUB_TOKEN are required", file=sys.stderr)
        return 2
    since = (
        datetime.now(timezone.utc) - timedelta(days=args.since_days)
        if args.since_days
        else None
    )
    records = list_records(
        args.repo,
        token,
        env=args.env,
        subject=args.subject,
        phase=args.phase,
        last=args.last,
        since=since,
    )
    print(json.dumps(records, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
