"""`python -m tools.diagnose` prints four sections from the shared query builders (#1651)."""

from __future__ import annotations

import ast
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
    if "severity_text" in query:
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


def test_error_log_levels_have_one_definition() -> None:
    """Only `signoz_queries.py` may list the error severities (#1651)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    owner = root / "common/observability/signoz_queries.py"
    levels = frozenset(q.ERROR_LOG_LEVELS)

    def has_copy(path: Path) -> bool:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.Tuple, ast.List, ast.Set)) and node.elts:
                values = [e.value for e in node.elts if isinstance(e, ast.Constant)]
                if len(values) == len(node.elts) and set(values) == levels:
                    return True
        return False

    hits = [
        str(path.relative_to(root))
        for base in ("common", "tools")
        for path in (root / base).rglob("*.py")
        if path != owner and "tests" not in path.parts and has_copy(path)
    ]
    assert hits == []
    assert q.ERROR_LOG_LEVELS == ("ERROR", "CRITICAL", "FATAL")
    assert "'ERROR', 'CRITICAL', 'FATAL'" in q.error_logs_query(
        service_name="svc", environment="staging"
    )
