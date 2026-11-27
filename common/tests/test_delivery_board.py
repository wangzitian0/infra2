"""The delivery board: the maths, the card, the SigNoz record and the reads (#1644 Q1)."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from common.observability import delivery_board as db

NOW = datetime(2026, 11, 27, 9, 0, tzinfo=timezone.utc)


def _run(branch: str, conclusion: str, created: str, event: str = "push") -> dict:
    return {
        "event": event,
        "head_branch": branch,
        "conclusion": conclusion,
        "created_at": created,
        "updated_at": created,
        "display_title": f"Reconcile IaC Inputs (staging {branch})",
    }


def _board(**changes) -> db.Board:
    values = dict(
        at=NOW,
        staging_passed=5,
        staging_runs=20,
        lead_p50_hours=6.2,
        lead_p90_hours=28.4,
        lead_prs=161,
        queue_prs=79,
        queue_oldest_hours=47.0,
        merged_prs=216,
        version_only_prs=52,
        test_lines=128,
        code_lines=100,
    )
    return db.Board(**{**values, **changes})


def test_a_pr_is_version_only_when_each_path_is_a_pin_file() -> None:
    assert db.is_version_only(("harness/repos.yaml", "repos/truealpha"))
    assert db.is_version_only(("pyproject.toml", "uv.lock"))
    assert not db.is_version_only(("harness/repos.yaml", "common/deploy/v2.py"))
    assert not db.is_version_only(())
    assert not db.is_version_only(None)


def test_staging_trains_are_the_newest_finished_tag_push_runs() -> None:
    runs = [
        _run("v3.2.5", "success", "2026-11-27T05:00:00Z"),
        _run("v3.2.4", "failure", "2026-11-27T04:00:00Z"),
        _run("v3.2.3", "cancelled", "2026-11-27T03:00:00Z"),
        _run("main", "success", "2026-11-27T02:00:00Z", event="workflow_dispatch"),
        _run("v3.2.2", "timed_out", "2026-11-27T01:00:00Z"),
        _run("v3.2.1", "success", "2026-11-27T00:00:00Z"),
    ]
    assert db.staging_trains(runs) == (2, 4)
    assert db.staging_trains(runs, last=2) == (1, 2)


def test_a_promotion_is_a_green_prod_run_of_a_tag_that_has_a_marker() -> None:
    def prod(tag: str, conclusion: str, updated: str) -> dict:
        run = _run("main", conclusion, updated, event="workflow_dispatch")
        return {**run, "display_title": f"Reconcile IaC Inputs (PROD {tag})"}

    runs = [
        prod("v3.2.1", "failure", "2026-11-25T05:00:00Z"),
        prod("v3.2.1", "success", "2026-11-25T07:27:00Z"),
        prod("v3.2.1", "success", "2026-11-25T09:00:00Z"),
        prod("v3.2.9", "success", "2026-11-26T09:00:00Z"),
    ]
    assert db.promotions(runs, ["production/v3.2.1"]) == {
        "v3.2.1": datetime(2026, 11, 25, 7, 27, tzinfo=timezone.utc)
    }


def test_lead_times_cover_the_promotions_of_the_window_and_the_queue() -> None:
    def hours_ago(hours: float) -> datetime:
        return NOW - timedelta(hours=hours)

    released = [
        (hours_ago(400), hours_ago(399)),  # older than the window
        (hours_ago(50), hours_ago(40)),
        (hours_ago(20), hours_ago(10)),
    ]
    merged = [
        hours_ago(500),  # not newer than the oldest tag: left out
        hours_ago(390),  # in the tag of 50 h ago: 350 h
        hours_ago(60),  # 20 h
        hours_ago(30),  # in the tag of 20 h ago: 20 h
        hours_ago(22),  # 12 h
        hours_ago(5),  # waits
        hours_ago(2),  # waits
    ]
    assert db.lead_times(merged, released, now=NOW) == {
        "lead_p50_hours": 20.0,
        "lead_p90_hours": 350.0,
        "lead_prs": 4,
        "queue_prs": 2,
        "queue_oldest_hours": 5.0,
    }
    # One promotion in the lookback: its PRs count. The merge commit of the head PR has
    # the commit time of the tag.
    single = db.lead_times([hours_ago(30), hours_ago(20)], released[-1:], now=NOW)
    assert (single["lead_prs"], single["lead_p90_hours"]) == (2, 20.0)
    # No promotion in the lookback: each PR waits. The queue is never empty by default.
    none = db.lead_times(merged, [], now=NOW)
    assert (none["lead_p90_hours"], none["queue_prs"]) == (None, 7)
    assert none["queue_oldest_hours"] == 500.0


def test_the_card_names_each_number_its_goal_and_the_signoz_state() -> None:
    rows = dict(db.board_rows(_board()))
    assert rows["Staging 通过率（近 20 趟）"] == "❌ 25%（5/20） · 目标 ≥67%"
    assert rows["等待上线"] == "79 个 PR · 最久 47.0 小时"
    assert rows["新应用从零到 Staging 绿"] == "❔ 未实测 · 目标 ≤4 小时"
    green = dict(db.board_rows(_board(staging_passed=14)))
    assert green["Staging 通过率（近 20 趟）"].startswith("✅ 70%")
    empty = dict(db.board_rows(_board(staging_runs=0, staging_passed=0)))
    assert empty["Staging 通过率（近 20 趟）"].startswith("❔ 没有发车记录")

    card = json.dumps(
        db.board_card(_board(), repo="o/r", signoz_error="URLError"), ensure_ascii=False
    )
    assert "交付日报 2026-11-27 17:00 UTC+8" in card
    assert "SigNoz 记录：未写入（URLError）" in card


def test_the_signoz_record_carries_each_measured_number() -> None:
    log = db.otlp_log(_board(), sha="a" * 40)["resourceLogs"][0]
    resource = {
        a["key"]: a["value"]["stringValue"] for a in log["resource"]["attributes"]
    }
    assert resource["service.name"] == "infra2-delivery-board"
    assert resource["infra.service.id"] == "harness/delivery_board"
    assert resource["infra.iac.ref"] == "a" * 40
    record = log["scopeLogs"][0]["logRecords"][0]
    numbers = {a["key"]: a["value"] for a in record["attributes"]}
    assert numbers["delivery.staging_pass_rate"] == {"doubleValue": 0.25}
    assert numbers["delivery.queue_prs"] == {"intValue": "79"}
    assert numbers["delivery.test_code_ratio"] == {"doubleValue": 1.28}
    assert "delivery.new_app_hours" not in numbers


def test_collect_board_reads_the_runs_the_markers_the_prs_and_the_tree(
    monkeypatch,
) -> None:
    def prod(tag: str, done: str) -> dict:
        run = _run("main", "success", done, event="workflow_dispatch")
        return {**run, "display_title": f"Reconcile IaC Inputs (PROD {tag})"}

    def pr(merged: str, *paths: str, commit: str = "") -> dict:
        files = {"totalCount": len(paths), "nodes": [{"path": p} for p in paths]}
        node = {"mergedAt": merged, "updatedAt": merged, "files": files}
        return {**node, "mergeCommit": {"committedDate": commit}} if commit else node

    prs = [
        pr("2026-11-27T08:00:00Z", "harness/repos.yaml", "repos/truealpha"),
        # The head PR of v3.2.1: GitHub sets mergedAt 2 s after the merge commit.
        pr(
            "2026-11-26T09:00:02Z", "common/deploy/v2.py", commit="2026-11-26T09:00:00Z"
        ),
        pr("2026-11-01T00:00:00Z", "uv.lock"),  # older than the lookback: ends the scan
    ]
    replies = {
        "runs?event=push": {
            "workflow_runs": [_run("v3.2.2", "success", "2026-11-27T01:00:00Z")]
        },
        "runs?event=workflow_dispatch": {
            "workflow_runs": [
                prod("v3.2.1", "2026-11-26T10:00:00Z"),
                prod("v3.2.0", "2026-11-18T01:00:00Z"),
            ]
        },
        "matching-refs/tags/production/": [
            {"ref": "refs/tags/production/v3.2.1"},
            {"ref": "refs/tags/production/v3.2.0"},
        ],
        "commits/v3.2.1": {"commit": {"committer": {"date": "2026-11-26T09:00:00Z"}}},
        "commits/v3.2.0": {"commit": {"committer": {"date": "2026-11-18T00:00:00Z"}}},
        "graphql": {
            "data": {
                "repository": {
                    "pullRequests": {
                        "pageInfo": {"hasNextPage": True, "endCursor": "c"},
                        "nodes": prs,
                    }
                }
            }
        },
    }
    seen: list[str] = []

    def opener(request, timeout=0):
        url = request.full_url
        seen.append(url)
        reply = next(body for part, body in replies.items() if part in url)
        return io.BytesIO(json.dumps(reply).encode())

    monkeypatch.setattr(db, "python_lines", lambda root: (128, 100))
    board = db.collect_board("o/r", "token", now=NOW, opener=opener)

    assert (board.staging_passed, board.staging_runs) == (1, 1)
    assert (board.lead_prs, board.lead_p90_hours) == (1, 1.0)
    assert (board.queue_prs, board.queue_oldest_hours) == (1, 1.0)
    assert (board.merged_prs, board.version_only_prs) == (2, 1)
    assert (board.test_lines, board.code_lines) == (128, 100)
    assert sum("graphql" in url for url in seen) == 1
    assert any("created=%3E%3D2026-11-06" in url for url in seen)


def test_python_lines_split_the_tracked_files_into_test_and_code(tmp_path) -> None:
    import subprocess

    for path, lines in {
        "common/deploy/v2.py": 3,
        "common/tests/test_v2.py": 5,
        "conftest.py": 1,
        "repos/app/main.py": 9,
        "notes.md": 7,
    }.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n" * lines)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

    assert db.python_lines(tmp_path) == (6, 3)


def test_a_scan_that_does_not_reach_the_lookback_raises() -> None:
    node = {"mergedAt": "2026-11-27T08:00:00Z", "updatedAt": "2026-11-27T08:00:00Z"}
    page = {
        "pageInfo": {"hasNextPage": True, "endCursor": "c"},
        "nodes": [{**node, "files": None}],
    }
    reply = json.dumps({"data": {"repository": {"pullRequests": page}}}).encode()
    calls = []

    def opener(request, timeout=0):
        calls.append(request.full_url)
        return io.BytesIO(reply)

    with pytest.raises(RuntimeError, match="more than 1000 PRs"):
        db.fetch_merged_prs(
            "o/r", "token", since=NOW - timedelta(days=21), opener=opener
        )
    assert len(calls) == db.MAX_PAGES


def test_a_board_that_cannot_be_computed_sends_a_card_and_does_not_fail(
    monkeypatch,
) -> None:
    from tools import delivery_board as tool

    sent = []
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.delenv("DELIVERY_BOARD_DRY_RUN", raising=False)
    monkeypatch.setattr(
        tool, "deliver_report_card", lambda env, card, **kw: sent.append(card)
    )

    def refused(*args, **kwargs):
        raise RuntimeError("GitHub GraphQL: rate limited")

    monkeypatch.setattr(tool, "collect_board", refused)
    assert tool.main() == 0
    assert "未能计算" in sent[0]["header"]["title"]["content"]
    assert "RuntimeError: GitHub GraphQL: rate limited" in json.dumps(
        sent[0], ensure_ascii=False
    )

    monkeypatch.setattr(tool, "collect_board", lambda *a, **kw: _board())
    monkeypatch.setattr(tool, "write_signoz", lambda board, env: "")
    assert tool.main() == 0
    assert "SigNoz 记录：已写入" in json.dumps(sent[1], ensure_ascii=False)
