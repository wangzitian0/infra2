from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from common.core import harness_cli
from common.core.harness import prod_queue
from common.core.harness.prod_queue import (
    PROD_TITLE,
    ProductionQueue,
    ProductionRun,
    preflight,
    read_production_queue,
    render_queue_lines,
)
from common.core.harness.status import observe_online, render_online_table
from common.tests.test_harness_status import (
    BEHIND,
    BODIES,
    FakeFetcher,
    _manifest,
    _markers,
    _status,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def _run(run_id: int, title: str, status: str, created: str) -> dict:
    return {
        "id": run_id,
        "display_title": title,
        "status": status,
        "created_at": created,
        "html_url": f"https://github.com/o/r/actions/runs/{run_id}",
    }


WAITING = _run(
    111, "Reconcile IaC Inputs (PROD v3.1.32)", "waiting", "2026-11-10T10:31:00Z"
)
PENDING = _run(
    222, "Reconcile IaC Inputs (PROD v3.1.33)", "pending", "2026-11-10T11:50:00Z"
)
STAGING = _run(
    333,
    "Reconcile IaC Inputs (staging v3.1.33)",
    "in_progress",
    "2026-11-10T11:55:00Z",
)


def _runner(
    by_status: dict[str, list[dict]] | None = None, *, returncode=0, stdout=None
):
    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        assert kwargs["cwd"] == ROOT
        if returncode != 0:
            return subprocess.CompletedProcess(argv, returncode, "", "boom")
        if stdout is not None:
            return subprocess.CompletedProcess(argv, 0, stdout, "")
        status = re.search(r"status=(\w+)", argv[2]).group(1)
        body = json.dumps({"workflow_runs": (by_status or {}).get(status, [])})
        return subprocess.CompletedProcess(argv, 0, body, "")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def _read(runner) -> ProductionQueue:
    return read_production_queue(ROOT, runner=runner, now=lambda: NOW)


def test_queue_keeps_only_production_runs_of_every_active_status() -> None:
    runner = _runner(
        {"waiting": [WAITING], "pending": [PENDING], "in_progress": [STAGING]}
    )
    queue = _read(runner)
    assert [(r.run_id, r.tag, r.status) for r in queue.runs] == [
        (111, "v3.1.32", "waiting"),
        (222, "v3.1.33", "pending"),
    ]
    assert queue.runs[0].age_minutes == 89
    assert queue.error == ""
    assert len(runner.calls) == len(prod_queue.ACTIVE_STATUSES)
    queried = {re.search(r"status=(\w+)", c[2]).group(1) for c in runner.calls}
    assert queried == {"requested", "queued", "pending", "waiting", "in_progress"}
    lines = render_queue_lines(queue)
    assert lines[0].startswith(
        "prod_queue: run 111 PROD v3.1.32 status=waiting age=89m"
    )
    assert "run 222 PROD v3.1.33 status=pending" in lines[1]
    code, message = preflight(queue)
    assert code == 1
    text = "\n".join(message)
    assert "111" in text and "222" in text
    assert "gh run cancel <id> --repo wangzitian0/infra2" in text


def test_empty_queue_allows_dispatch() -> None:
    queue = _read(_runner({"in_progress": [STAGING]}))
    assert render_queue_lines(queue) == ["prod_queue: empty"]
    assert preflight(queue)[0] == 0


@pytest.mark.parametrize(
    "runner",
    [_runner(returncode=1), _runner(stdout="not json"), _runner(stdout="{}")],
    ids=["gh-failure", "bad-json", "missing-key"],
)
def test_unreadable_queue_is_never_empty(runner) -> None:
    queue = _read(runner)
    lines = render_queue_lines(queue)
    assert "UNREADABLE" in lines[0]
    assert "empty" not in lines[0]
    assert preflight(queue)[0] == 4


def test_same_run_in_two_statuses_is_listed_once() -> None:
    queue = _read(_runner({"waiting": [WAITING], "pending": [WAITING]}))
    assert [r.run_id for r in queue.runs] == [111]


def test_run_name_contract_matches_workflow_file() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/reconcile-iac-inputs.yml").read_text("utf-8")
    )
    template = workflow["run-name"]

    def render(promote: bool, after: str) -> str:
        def substitute(match: re.Match) -> str:
            expression = match.group(1).strip()
            if expression == "inputs.promote_prod && 'PROD' || 'staging'":
                return "PROD" if promote else "staging"
            if expression == "inputs.after || github.ref_name":
                return after or "refs"
            raise AssertionError(f"unhandled expression: {expression}")

        return re.sub(r"\$\{\{(.*?)\}\}", substitute, template)

    prod = PROD_TITLE.search(render(True, "v9.9.9"))
    assert prod is not None and prod.group("tag") == "v9.9.9"
    assert PROD_TITLE.search(render(False, "v9.9.9")) is None


def test_online_table_includes_queue_lines_when_reader_is_passed(
    tmp_path: Path,
) -> None:
    queue = ProductionQueue(
        runs=(
            ProductionRun(111, "v3.1.32", "waiting", "2026-11-10T10:31:00Z", 89, "u"),
        )
    )

    def observe(reader):
        return observe_online(
            _status(),
            tmp_path,
            _manifest(),
            fetcher=FakeFetcher(BODIES),
            marker_status=_markers(BEHIND),
            now=lambda: NOW,
            queue_reader=reader,
        )

    shown = observe(lambda root: queue)
    assert "prod_queue: run 111 PROD v3.1.32 status=waiting age=89m u" in (
        render_online_table(shown)
    )
    assert shown.to_dict()["prod_queue"]["runs"][0]["run_id"] == 111
    hidden = observe(None)
    assert not any(
        line.startswith("prod_queue") for line in render_online_table(hidden)
    )
    assert "prod_queue" not in hidden.to_dict()


@pytest.mark.parametrize(
    ("queue", "expected"),
    [
        (ProductionQueue(), 0),
        (
            ProductionQueue(
                runs=(
                    ProductionRun(
                        1, "v1.0.0", "waiting", "2026-11-10T10:00:00Z", 5, "u"
                    ),
                )
            ),
            1,
        ),
        (ProductionQueue(error="OSError: no gh"), 4),
    ],
    ids=["empty", "occupied", "unreadable"],
)
def test_cli_prod_queue_exit_code(monkeypatch, capsys, queue, expected) -> None:
    monkeypatch.setattr(
        harness_cli.harness_prod_queue, "read_production_queue", lambda root: queue
    )
    assert harness_cli.main(["prod-queue", "--root", str(ROOT)]) == expected
    assert "prod_queue:" in capsys.readouterr().out
    assert harness_cli.main(["prod-queue", "--root", str(ROOT), "--json"]) == expected
    assert json.loads(capsys.readouterr().out)["exit"] == expected
