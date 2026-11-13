"""`python -m tools.diagnose` prints four sections from the shared query builders (#1651)."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone

import pytest

from common.observability import signoz_queries as q
from tools import diagnose

_GIT_ENV = {
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
}


def _fake_run(query: str) -> str:
    if "countIf" in query:
        return "200\t5\n"
    if "ORDER BY duration_nano" in query:
        return "abc123\t812.5\tGET /api/x\t200\n"
    if "severity_number" in query:
        return "2026-11-13 01:00:00\tdeadbeef\tboom happened\n"
    raise AssertionError(query)


def _git_repo(path, tags: dict[str, str]):
    def git(*args, env=None):
        subprocess.run(
            ["git", *args], cwd=path, check=True, env=env, capture_output=True
        )

    git("init", "-q")
    git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
    )
    for tag, when in tags.items():
        git("tag", "-a", tag, "-m", tag, env={**_GIT_ENV, "GIT_COMMITTER_DATE": when})
    return path


def test_report_has_four_sections_with_trace_ids(tmp_path) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    repo = _git_repo(tmp_path, {"v1.2.3": now, "v0.0.1": "2020-01-01T00:00:00+00:00"})
    out = diagnose.diagnose("svc", "staging", 3600, run=_fake_run, repo_root=repo)
    assert out.count("== ") == 4
    assert "trace=deadbeef  boom happened" in out
    assert "trace=abc123  812.5 ms" in out
    assert "5 of 200 root spans: 2.50%" in out
    assert "v1.2.3" in out and "v0.0.1" not in out


def test_since_parser_rejects_bad_window() -> None:
    assert diagnose.parse_since("30m") == 1800
    for bad in ("0m", "30", "m", "1w"):
        with pytest.raises(ValueError):
            diagnose.parse_since(bad)


def test_builders_scope_by_service_environment_and_reject_unsafe_input() -> None:
    for build in (q.error_logs_query, q.slow_traces_query, q.status_5xx_query):
        sql = build(service_name="svc", environment="staging", window_seconds=60)
        assert "'svc'" in sql and "'staging'" in sql and "60 SECOND" in sql
        with pytest.raises(ValueError):
            build(service_name="a'; --", environment="staging")
