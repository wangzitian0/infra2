"""Diagnose one service: error logs, slow traces, 5xx rate and deploys in one window.

Usage: python -m tools.diagnose <service> --env staging|production --since 30m

`<service>` is the OTEL `service.name`, for example `finance-report-backend`.
The command reads ClickHouse over SSH, the same path as `tools.canary_verify`.
The command only reads telemetry and git tags. It writes nothing.
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common.deploy.vps_ssh import resolve_ssh_credentials, ssh_cmd
from common.deploy.release_markers import PRODUCTION_MARKER_PREFIX, RELEASE_TAG_RE
from common.observability.signoz_queries import (
    error_logs_query,
    slow_traces_query,
    status_5xx_query,
)

_SINCE_RE = re.compile(r"\A([0-9]+)([smhd])\Z")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_CLICKHOUSE = "platform-clickhouse"

Runner = Callable[[str], str]


def parse_since(text: str) -> int:
    """Return the window in seconds for `30m`, `1h`, `45s` or `2d`."""
    match = _SINCE_RE.fullmatch(text)
    if not match or int(match.group(1)) == 0:
        raise ValueError(f"--since must look like 30m, 1h or 2d: {text!r}")
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)]


def ssh_runner() -> Runner:
    host, user, key_path, port = resolve_ssh_credentials()

    def run(query: str) -> str:
        command = (
            f"docker exec {_CLICKHOUSE} clickhouse-client --query {shlex.quote(query)}"
        )
        code, stdout, stderr = ssh_cmd(command, host, user, key_path, port)
        if code != 0:
            raise RuntimeError(f"ClickHouse query failed: {stderr.strip()[:300]}")
        return stdout

    return run


def _rows(output: str) -> list[list[str]]:
    return [line.split("\t") for line in output.splitlines() if line.strip()]


def deploys_in_window(
    *, window_seconds: int, repo_root: str | Path, now: datetime | None = None
) -> list[tuple[str, str, str]]:
    """Return (tag, time, commit) for release tags and production markers in the window."""
    result = subprocess.run(
        [
            "git",
            "for-each-ref",
            "refs/tags",
            "--sort=-creatordate",
            "--format=%(refname:short)\t%(creatordate:iso-strict)\t"
            "%(objectname:short)\t%(*objectname:short)",
        ],
        cwd=Path(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=window_seconds)
    found: list[tuple[str, str, str]] = []
    for line in result.stdout.splitlines():
        tag, when, obj, commit = (line.split("\t") + [""] * 4)[:4]
        if not RELEASE_TAG_RE.fullmatch(tag.removeprefix(PRODUCTION_MARKER_PREFIX)):
            continue
        if datetime.fromisoformat(when) >= cutoff:
            found.append((tag, when, commit or obj))
    return found


def diagnose(
    service: str,
    environment: str,
    window_seconds: int,
    *,
    run: Runner,
    repo_root: str | Path = ".",
) -> str:
    """Return the four report sections as text."""
    args = {
        "service_name": service,
        "environment": environment,
        "window_seconds": window_seconds,
    }
    lines = [f"== Error logs ({service}, {environment}, last {window_seconds}s)"]
    logs = _rows(run(error_logs_query(**args)))
    lines += [
        f"{r[0]}  trace={r[1] or '-'}  {r[2] if len(r) > 2 else ''}" for r in logs
    ]
    if not logs:
        lines.append("none")

    lines.append("== Slowest traces")
    traces = _rows(run(slow_traces_query(**args)))
    lines += [
        f"trace={r[0]}  {r[1]} ms  {r[2]}  status={r[3] if len(r) > 3 else ''}"
        for r in traces
    ]
    if not traces:
        lines.append("none")

    lines.append("== 5xx rate")
    counts = _rows(run(status_5xx_query(**args)))
    total, errors = (int(counts[0][0]), int(counts[0][1])) if counts else (0, 0)
    rate = f"{100 * errors / total:.2f}%" if total else "n/a (no requests)"
    lines.append(f"{errors} of {total} root spans: {rate}")

    lines.append("== Deploys in window (tag, time, commit)")
    deploys = deploys_in_window(window_seconds=window_seconds, repo_root=repo_root)
    lines += [f"{tag}  {when}  {commit}" for tag, when, commit in deploys]
    if not deploys:
        lines.append("none")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("--env", required=True, choices=("staging", "production"))
    parser.add_argument("--since", default="30m")
    args = parser.parse_args(argv)
    try:
        window = parse_since(args.since)
        print(diagnose(args.service, args.env, window, run=ssh_runner()))
    except (ValueError, RuntimeError) as exc:
        print(f"diagnose: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
