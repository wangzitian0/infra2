#!/usr/bin/env python3
"""Read the stage records of this repository that the Actions artifacts hold (#1645, #1646).

A stage task uploads each record (``common/deploy/stage_record.py``) as the artifact
``stage-<env>-<phase>-<subject>``. ``list`` prints the records that pass the same provenance
checks as ``tools/ci_tree_reuse.py``. A record counts only when all of these are true:

1. The artifact is not expired.
2. The run that uploaded it is in this repository (not a fork).
3. The run is a run of the workflow that owns the env, with an event that the env allows.
4. The record names the same env and the same artifact name, and its ``run_url`` is that run.
5. The run built the commit of the subject (see ``_run_is_the_subject``).

``read`` judges the newest record of one env, phase and subject. It adds two checks: the run
event is the required one (``push`` for a tag soak: a dispatch run does not count) and the
commit of the subject is on ``main``. It judges the checks of the record, not its ``verdict``
field. The exit code is 0 for a pass, 1 for a refusal and 4 when it cannot decide.

The command lines are ``python -m tools.stage_records list`` and ``... read``.

Fail safe: ``list`` skips an artifact it doubts and writes the reason to stderr. ``read`` never
passes on a doubt. Standard library only: a step may run it before any dependency is installed.
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
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from common.deploy.github_api import Opener, get_json, github_headers

AGENT = "infra2-stage-records/1.0"
PREFIX = "stage-"
MAX_PAGES = 100
PHASES = ("pre", "operation", "post")
EXIT_PASS, EXIT_REFUSED, EXIT_UNREADABLE = 0, 1, 4
#: ``compare/<sha>...main`` says ``behind`` or ``identical`` when ``sha`` is on main.
ON_MAIN = ("behind", "identical")
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


def _check_artifact(
    artifact: dict, repo: str, token: str, opener: Opener
) -> tuple[dict, dict] | str:
    """``(record, run)`` of one artifact, or the reason that a provenance check fails."""
    origin = artifact.get("workflow_run")
    if artifact.get("expired") is not False:
        return "the artifact is expired"
    if not isinstance(origin, dict):
        return "the artifact names no run"
    run_id, repo_id = origin.get("id"), origin.get("repository_id")
    if (
        not isinstance(run_id, int)
        or repo_id is None
        or repo_id != origin.get("head_repository_id")
    ):
        return "the run is not in this repository (fork)"
    run = get_json(f"/repos/{repo}/actions/runs/{run_id}", token, opener, AGENT)
    archive = zipfile.ZipFile(
        io.BytesIO(_download(str(artifact["archive_download_url"]), token, opener))
    )
    files = [n for n in archive.namelist() if n.endswith(".json")]
    if len(files) != 1:
        return "the artifact does not hold one JSON file"
    record = json.loads(archive.read(files[0]).decode("utf-8"))
    if not isinstance(record, dict) or record.get("schema") != 1:
        return "the file is not a stage record"
    env, phase, subject = (
        record.get("env"),
        record.get("phase"),
        str(record.get("subject")),
    )
    writer = WRITERS.get(str(env))
    if writer is None or run.get("path") != writer[0]:
        return "the run is not a run of the workflow that owns the env"
    if run.get("event") not in writer[1]:
        return f"the run event {run.get('event')!r} is not allowed for the env"
    if (
        phase not in PHASES
        or artifact.get("name") != f"{PREFIX}{env}-{phase}-{subject.replace(':', '-')}"
        or not str(record.get("run_url", "")).endswith(f"/actions/runs/{run_id}")
        or not isinstance(record.get("checks"), list)
    ):
        return "the record does not match its artifact name or its run"
    if not _run_is_the_subject(run, subject, repo, token, opener):
        return "the run did not build the commit of the subject"
    return record, run


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


def _artifacts(repo: str, token: str, opener: Opener, name: str = "") -> Iterator[dict]:
    """Artifacts of the repository (only those with ``name`` when it is given), newest first."""
    for page in range(1, MAX_PAGES + 1):
        query = urllib.parse.urlencode(
            {"per_page": 100, "page": page, **({"name": name} if name else {})}
        )
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
            checked = _check_artifact(artifact, repo, token, opener)
        except Exception as exc:  # noqa: BLE001 - one bad artifact never hides the others
            print(
                f"stage_records: skip {name} ({type(exc).__name__}: {exc})",
                file=sys.stderr,
            )
            continue
        record = checked[0] if isinstance(checked, tuple) else None
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


@dataclass(frozen=True)
class Reading:
    """What ``read_record`` decided: an exit code, one line for the log, the record if any."""

    exit_code: int
    detail: str
    record: dict | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == EXIT_PASS


def read_record(
    repo: str,
    token: str,
    *,
    env: str,
    subject: str,
    phase: str = "post",
    event: str = "",
    on_main: bool = True,
    opener: Opener = open_url,
) -> Reading:
    """Judge the newest record of ``env``, ``phase`` and ``subject`` that passes every check.

    ``event`` is the event that the run must have (``push`` for a tag soak). ``on_main``
    requires the commit that the run built to be reachable from ``main``. A record that
    fails a check is skipped, so a newer dispatch run never hides an older push run. The
    record passes when every check in it is ``ok``; its ``verdict`` field is not read. An API
    error or an unreadable artifact gives ``EXIT_UNREADABLE``, never a pass.
    """
    label = f"{env} {subject} {phase}"
    name = f"{PREFIX}{env}-{phase}-{subject.replace(':', '-')}"
    skipped: list[str] = []
    try:
        for artifact in _artifacts(repo, token, opener, name):
            if artifact.get("name") != name:
                continue
            checked = _check_artifact(artifact, repo, token, opener)
            if isinstance(checked, str):
                skipped.append(checked)
                continue
            record, run = checked
            if event and run.get("event") != event:
                skipped.append(f"the run event is {run.get('event')!r}, not {event!r}")
                continue
            if on_main:
                sha = str(run.get("head_sha"))
                status = get_json(
                    f"/repos/{repo}/compare/{sha}...main", token, opener, AGENT
                ).get("status")
                if status not in ON_MAIN:
                    return Reading(
                        EXIT_REFUSED,
                        f"{label}: commit {sha[:12]} is not on main (compare status {status!r})",
                    )
            failed = [
                str(c.get("name"))
                for c in record["checks"]
                if not isinstance(c, dict) or c.get("ok") is not True
            ]
            if failed or not record["checks"]:
                return Reading(
                    EXIT_REFUSED,
                    f"{label}: failed check {', '.join(failed) or '(no check in the record)'}"
                    f" ({record.get('run_url')})",
                    record,
                )
            return Reading(
                EXIT_PASS, f"{label}: pass ({record.get('run_url')})", record
            )
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        return Reading(
            EXIT_UNREADABLE, f"{label}: cannot read ({type(exc).__name__}: {exc})"
        )
    reasons = "; ".join(sorted(set(skipped))) or "no artifact of that name"
    return Reading(EXIT_REFUSED, f"{label}: no record that counts ({reasons})")


def staging_gates_verdict(
    records: Sequence[Mapping[str, Any]], required: Mapping[str, str]
) -> tuple[bool, str]:
    """Every required ``(service, version_ref)`` has a green ``staging_gate`` check (#1563).

    ``records`` are the green post records of the staging soaks since the last production
    marker. The post writer starts the ``detail`` of a hook check with ``[<version_ref>]``.
    A service is missing when no record holds its check at that version, and failed when any
    such check is red.
    """
    missing: list[str] = []
    failed: list[str] = []
    for service, version in sorted(required.items()):
        svc, ver = str(service).strip(), str(version).strip()
        found = [
            check
            for record in records
            for check in record.get("checks", ())
            if check.get("name") == f"{svc}:staging_gate"
            and str(check.get("detail", "")).startswith(f"[{ver}]")
        ]
        if not found:
            missing.append(f"{svc}@{ver}")
        elif not all(check.get("ok") is True for check in found):
            failed.append(f"{svc}@{ver}")
    if missing or failed:
        parts = [
            f"{kind} staging_gate for {', '.join(items)}"
            for kind, items in (("missing", missing), ("failed", failed))
            if items
        ]
        return False, "; ".join(parts)
    return True, "staging_gate passed for " + (
        ", ".join(f"{s}@{v}" for s, v in sorted(required.items())) or "no app release"
    )


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
    one = sub.add_parser(
        "read", help="judge the newest record of one env, phase and subject"
    )
    one.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    one.add_argument("--env", required=True, help="full stage id")
    one.add_argument("--subject", required=True, help="e.g. tag:v1.2.3")
    one.add_argument("--phase", choices=PHASES, default="post")
    one.add_argument("--event", default="", help="the run event that counts, e.g. push")
    one.add_argument(
        "--any-commit",
        action="store_true",
        help="do not require the commit to be on main",
    )
    args = parser.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN", "")
    if not (args.repo and token):
        print("stage_records: --repo and GITHUB_TOKEN are required", file=sys.stderr)
        return 2
    if args.command == "read":
        reading = read_record(
            args.repo,
            token,
            env=args.env,
            subject=args.subject,
            phase=args.phase,
            event=args.event,
            on_main=not args.any_commit,
        )
        print(reading.detail)
        return reading.exit_code
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
