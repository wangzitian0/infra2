#!/usr/bin/env python3
# alerts-as: monthly-ops-summary
"""The delivery board: five numbers, one Lark card and one SigNoz log record (#1644 Q1).

The board reports. It gates nothing. Each number has one source:

1. Staging pass rate: the conclusions of the newest ``LAST_TRAINS`` finished tag-push runs
   of the train workflow (GitHub Actions run list).
2. Merge to production: the time from the merge of a PR to the end of the first production
   promotion that contains it, as p50 and p90 over the promotions of the window; and the
   PRs that wait now. A promotion is a green ``PROD`` run of the train workflow whose tag
   has a ``production/<tag>`` marker.
3. New App from zero to a green Staging: a timed run. ``NEW_APP_HOURS`` is ``None`` until
   one run exists.
4. Version-only PRs: the share of the merged PRs whose every changed path is a pin file.
5. Test lines divided by code lines of the tracked Python files.

The board reads no stage record: one record is one artifact download, and a daily read of
three weeks does not fit a 10-minute job. A run makes about 40 API calls (37 measured on
2026-11-27): the run list, the markers, one commit for each promoted tag, and the PR pages. ``GOALS`` holds the goal of each number.
``docs/ssot/ops.pipeline.md`` section 2.0 names the board; ``tools/delivery_board.py`` is
the entry point.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from common.alerting import build_feishu_report_card
from common.core.service_identity import ServiceIdentity
from common.deploy.github_api import API, get_json, github_headers, with_retry
from common.deploy.release_markers import PRODUCTION_MARKER_PREFIX, RELEASE_TAG_RE
from common.deploy.stage_record_reader import open_url
from common.observability.delivery_section import percentile
from common.observability.digest import DEFAULT_INTERNAL_DOMAIN, SIGNOZ_URL

ROOT = Path(__file__).resolve().parents[2]
AGENT = "infra2-delivery-board/1.0"
TRAIN_WORKFLOW = "reconcile-iac-inputs.yml"
#: The ``run-name`` of the train workflow names a production promotion this way.
PROD_TITLE = re.compile(r"\(PROD (v[0-9]+\.[0-9]+\.[0-9]+)\)")
LAST_TRAINS = 20
WINDOW_DAYS = 7
#: A promotion can contain a PR that is older than the window. The longest measured wait
#: is 5 days (2026-11); a PR that waits longer than the lookback is in no number.
LOOKBACK_DAYS = 14
#: Pages of 100 runs for one event.
MAX_PAGES = 20
#: Pages of 50 PRs: 3,000 PRs updated in the lookback (about 450 measured on 2026-11-27).
MAX_PR_PAGES = 60
PIN_PATHS = frozenset({"harness/repos.yaml", "pyproject.toml", "uv.lock"})
PIN_PREFIX = "repos/"
#: Set by the PR that records the first timed run of a new App (#1644 Q4).
NEW_APP_HOURS: float | None = None
GOALS = {
    "staging_pass_rate": 0.67,
    "lead_p90_hours": 24.0,
    "new_app_hours": 4.0,
    "version_only_share": 0.10,
    "test_code_ratio": 0.6,
}
BOARD_SERVICE_ID = "harness/delivery_board"
BOARD_SERVICE_NAME = "infra2-delivery-board"
BOARD_ENVIRONMENT = "ci"
#: The one public OTLP ingest (ops.observability.md section 4.4).
DEFAULT_OTLP_LOGS_URL = f"https://otel.{DEFAULT_INTERNAL_DOMAIN}/v1/logs"
UTC8 = timezone(timedelta(hours=8))
_TEST_PATH = re.compile(
    r"(^|/)tests?/|(^|/)test_[^/]*\.py$|(^|/)conftest\.py$|^e2e_regressions/"
)
_PRS_QUERY = (
    "query($owner:String!,$name:String!,$after:String){"
    "repository(owner:$owner,name:$name){"
    "pullRequests(states:MERGED,first:50,after:$after,"
    "orderBy:{field:UPDATED_AT,direction:DESC}){"
    "pageInfo{hasNextPage endCursor} "
    "nodes{mergedAt updatedAt mergeCommit{committedDate} "
    "files(first:50){totalCount nodes{path}}}}}}"
)


@dataclass(frozen=True)
class Board:
    """The measured numbers of one report. ``None`` means "not measured"."""

    at: datetime
    staging_passed: int
    staging_runs: int
    lead_p50_hours: float | None
    lead_p90_hours: float | None
    lead_prs: int
    queue_prs: int
    queue_oldest_hours: float
    merged_prs: int
    version_only_prs: int
    test_lines: int
    code_lines: int
    new_app_hours: float | None = NEW_APP_HOURS

    def numbers(self) -> dict[str, int | float | None]:
        """The flat numbers of the SigNoz record. A rate with no runs is ``None``."""

        def ratio(part: int, whole: int) -> float | None:
            return round(part / whole, 4) if whole else None

        return {
            "staging_pass_rate": ratio(self.staging_passed, self.staging_runs),
            "staging_passed": self.staging_passed,
            "staging_runs": self.staging_runs,
            "lead_p50_hours": self.lead_p50_hours,
            "lead_p90_hours": self.lead_p90_hours,
            "lead_prs": self.lead_prs,
            "queue_prs": self.queue_prs,
            "queue_oldest_hours": self.queue_oldest_hours,
            "merged_prs": self.merged_prs,
            "version_only_prs": self.version_only_prs,
            "version_only_share": ratio(self.version_only_prs, self.merged_prs),
            "test_lines": self.test_lines,
            "code_lines": self.code_lines,
            "test_code_ratio": ratio(self.test_lines, self.code_lines),
            "new_app_hours": self.new_app_hours,
            "window_days": WINDOW_DAYS,
        }


def _time(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def is_version_only(paths: Sequence[str] | None) -> bool:
    """True when every changed path is a pin file. ``None`` is an incomplete path list."""
    return bool(paths) and all(
        path in PIN_PATHS or path.startswith(PIN_PREFIX) for path in paths
    )


def staging_trains(
    runs: Iterable[Mapping[str, Any]], last: int = LAST_TRAINS
) -> tuple[int, int]:
    """(passed, finished) over the newest ``last`` finished tag-push runs.

    A cancelled run is not a finished run: a person or a newer run stopped it.
    """
    finished = [
        run
        for run in runs
        if run.get("event") == "push"
        and RELEASE_TAG_RE.match(str(run.get("head_branch") or ""))
        and run.get("conclusion") in ("success", "failure", "timed_out")
    ]
    newest = sorted(finished, key=lambda run: str(run["created_at"]), reverse=True)
    newest = newest[:last]
    return sum(run["conclusion"] == "success" for run in newest), len(newest)


def promotions(
    runs: Iterable[Mapping[str, Any]], marker_tags: Iterable[str]
) -> dict[str, datetime]:
    """Tag to the end of its first green production run. The tag must have a marker.

    The run list does not name the phase of a run. A green ``phase=pre`` run before the
    promotion of the same tag gives an end that is too early; a later replay does not.
    """
    marked = {tag.removeprefix(PRODUCTION_MARKER_PREFIX) for tag in marker_tags}
    found: dict[str, datetime] = {}
    for run in runs:
        match = PROD_TITLE.search(str(run.get("display_title") or ""))
        if match and run.get("conclusion") == "success" and match.group(1) in marked:
            tag, at = match.group(1), _time(run["updated_at"])
            found[tag] = min(found.get(tag, at), at)
    return found


def lead_times(
    merged: Sequence[datetime],
    released: Sequence[tuple[datetime, datetime]],
    *,
    now: datetime,
    window_days: int = WINDOW_DAYS,
) -> dict[str, Any]:
    """Merge-to-production hours and the queue.

    ``merged`` holds the commit time of each merge. ``released`` holds one (commit time of
    the tag, end of the promotion) pair for each promoted tag. A PR is in the first promoted
    tag whose commit is not older than its merge commit. Both lists cover the same lookback,
    and a promotion follows the merges that it contains, so each promotion of a listed PR
    is in ``released``. A PR with no such tag waits; with no promotion, each PR waits. The
    p50 and p90 cover the promotions of the window. A PR that is older than the lookback
    is in neither list.
    """
    cuts = sorted(released)
    since = now - timedelta(days=window_days)
    hours: list[float] = []
    waiting: list[datetime] = []
    for at in merged:
        hit = next(((cut, done) for cut, done in cuts if cut >= at), None)
        if hit is None:
            waiting.append(at)
        elif hit[1] >= since:
            hours.append((hit[1] - at).total_seconds() / 3600)
    return {
        "lead_p50_hours": round(percentile(hours, 0.5), 1) if hours else None,
        "lead_p90_hours": round(percentile(hours, 0.9), 1) if hours else None,
        "lead_prs": len(hours),
        "queue_prs": len(waiting),
        "queue_oldest_hours": round(
            max(((now - at).total_seconds() / 3600 for at in waiting), default=0.0), 1
        ),
    }


def python_lines(root: Path = ROOT) -> tuple[int, int]:
    """(test lines, code lines) of the tracked Python files outside ``repos/``."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"],
        cwd=root,
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout.decode("utf-8")
    test = code = 0
    for path in filter(None, listed.split("\0")):
        if path.startswith(PIN_PREFIX):
            continue
        with open(root / path, "rb") as handle:
            lines = sum(1 for _ in handle)
        if _TEST_PATH.search(path):
            test += lines
        else:
            code += lines
    return test, code


def _post_json(
    url: str, payload: Mapping[str, Any], headers: Mapping[str, str], opener: Callable
) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )

    def call() -> Any:
        with opener(request, timeout=20) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body.strip() else {}

    return with_retry(call)


def fetch_train_runs(
    repo: str,
    token: str,
    *,
    event: str,
    since: datetime | None = None,
    opener: Callable = open_url,
) -> list[dict]:
    """The runs of the train workflow for one event, newest first.

    Without ``since`` it reads one page of 100 runs. With ``since`` it reads each page.
    """
    query = f"event={event}&per_page=100"
    if since:
        query += f"&created=%3E%3D{since:%Y-%m-%d}"
    runs: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        reply = get_json(
            f"/repos/{repo}/actions/workflows/{TRAIN_WORKFLOW}/runs?{query}&page={page}",
            token,
            opener,
            AGENT,
        )
        runs += reply["workflow_runs"]
        if not since or len(reply["workflow_runs"]) < 100:
            break
    return runs


def fetch_merged_prs(
    repo: str, token: str, *, since: datetime, opener: Callable = open_url
) -> list[tuple[datetime, tuple[str, ...] | None]]:
    """(commit time of the merge, changed paths) of each PR merged since ``since``.

    The time is the commit date of the merge commit: GitHub sets ``mergedAt`` 1 to 2
    seconds later, and the tag of a train points at that commit. The paths are ``None``
    when the PR changes more files than one page holds. A scan that does not reach
    ``since`` in ``MAX_PR_PAGES`` pages raises: a short list gives wrong numbers.
    """
    owner, _, name = repo.partition("/")
    found: list[tuple[datetime, tuple[str, ...] | None]] = []
    after: str | None = None
    for _ in range(MAX_PR_PAGES):
        reply = _post_json(
            f"{API}/graphql",
            {
                "query": _PRS_QUERY,
                "variables": {"owner": owner, "name": name, "after": after},
            },
            github_headers(token, AGENT),
            opener,
        )
        if reply.get("errors"):
            raise RuntimeError(f"GitHub GraphQL: {reply['errors'][0].get('message')}")
        page = reply["data"]["repository"]["pullRequests"]
        for node in page["nodes"]:
            if _time(node["mergedAt"]) >= since:
                commit = node.get("mergeCommit") or {}
                files = node["files"] or {"totalCount": 0, "nodes": []}
                paths = tuple(f["path"] for f in files["nodes"])
                complete = files["totalCount"] == len(paths)
                found.append(
                    (
                        _time(commit.get("committedDate") or node["mergedAt"]),
                        paths if complete else None,
                    )
                )
        # The order is the update time. A page that ends before ``since`` ends the scan.
        if not page["nodes"] or _time(page["nodes"][-1]["updatedAt"]) < since:
            return found
        if not page["pageInfo"]["hasNextPage"]:
            return found
        after = page["pageInfo"]["endCursor"]
    raise RuntimeError(f"more than {MAX_PR_PAGES * 50} PRs since {since:%Y-%m-%d}")


def collect_board(
    repo: str,
    token: str,
    *,
    now: datetime | None = None,
    root: Path = ROOT,
    opener: Callable = open_url,
) -> Board:
    """Read the train runs, the markers, the merged PRs and the tree; compute the board."""
    now = now or datetime.now(timezone.utc)
    lookback = now - timedelta(days=LOOKBACK_DAYS)
    passed, finished = staging_trains(
        fetch_train_runs(repo, token, event="push", opener=opener)
    )
    markers = get_json(
        f"/repos/{repo}/git/matching-refs/tags/{PRODUCTION_MARKER_PREFIX}",
        token,
        opener,
        AGENT,
    )
    promoted = promotions(
        fetch_train_runs(
            repo, token, event="workflow_dispatch", since=lookback, opener=opener
        ),
        [ref["ref"].removeprefix("refs/tags/") for ref in markers],
    )
    released = []
    for tag, at in promoted.items():
        commit = get_json(f"/repos/{repo}/commits/{tag}", token, opener, AGENT)
        released.append((_time(commit["commit"]["committer"]["date"]), at))
    prs = fetch_merged_prs(repo, token, since=lookback, opener=opener)
    window = [paths for at, paths in prs if at >= now - timedelta(days=WINDOW_DAYS)]
    test, code = python_lines(root)
    return Board(
        at=now,
        staging_passed=passed,
        staging_runs=finished,
        merged_prs=len(window),
        version_only_prs=sum(map(is_version_only, window)),
        test_lines=test,
        code_lines=code,
        **lead_times([at for at, _ in prs], released, now=now),
    )


def _mark(value: float | None, goal: float, *, at_least: bool = False) -> str:
    if value is None:
        return "❔"
    return "✅" if (value >= goal if at_least else value <= goal) else "❌"


def board_rows(board: Board) -> list[tuple[str, str]]:
    """One (label, value) row for each number. The value names its goal."""
    n = board.numbers()
    rate, share, ratio = (
        n["staging_pass_rate"],
        n["version_only_share"],
        n["test_code_ratio"],
    )
    staging = (
        f"{rate * 100:.0f}%（{board.staging_passed}/{board.staging_runs}）"
        if rate is not None
        else "没有发车记录"
    )
    lead = (
        f"{board.lead_p90_hours:.1f} 小时（中位 {board.lead_p50_hours:.1f}，"
        f"{board.lead_prs} 个 PR）"
        if board.lead_p90_hours is not None
        else f"近 {WINDOW_DAYS} 天没有上线"
    )
    versions = (
        f"{share * 100:.0f}%（{board.version_only_prs}/{board.merged_prs}）"
        if share is not None
        else "没有合并的 PR"
    )
    new_app = (
        f"{board.new_app_hours:.1f} 小时"
        if board.new_app_hours is not None
        else "未实测"
    )
    return [
        (
            f"Staging 通过率（近 {LAST_TRAINS} 趟）",
            f"{_mark(rate, GOALS['staging_pass_rate'], at_least=True)} {staging} · "
            f"目标 ≥{GOALS['staging_pass_rate'] * 100:.0f}%",
        ),
        (
            f"合并到上线 P90（近 {WINDOW_DAYS} 天）",
            f"{_mark(board.lead_p90_hours, GOALS['lead_p90_hours'])} {lead} · "
            f"目标 ≤{GOALS['lead_p90_hours']:.0f} 小时",
        ),
        (
            "等待上线",
            f"{board.queue_prs} 个 PR · 最久 {board.queue_oldest_hours:.1f} 小时",
        ),
        (
            f"只改版本号的 PR（近 {WINDOW_DAYS} 天）",
            f"{_mark(share, GOALS['version_only_share'])} {versions} · "
            f"目标 ≤{GOALS['version_only_share'] * 100:.0f}%",
        ),
        (
            "测试行数 ÷ 代码行数",
            f"{_mark(ratio, GOALS['test_code_ratio'])} "
            f"{f'{ratio:.2f}' if ratio is not None else '无代码'}"
            f"（{board.test_lines}/{board.code_lines}）· 目标 ≤{GOALS['test_code_ratio']}",
        ),
        (
            "新应用从零到 Staging 绿",
            f"{_mark(board.new_app_hours, GOALS['new_app_hours'])} {new_app} · "
            f"目标 ≤{GOALS['new_app_hours']:.0f} 小时",
        ),
    ]


def board_lines(board: Board) -> list[str]:
    """The board as text lines, for the log and the fallback text."""
    return [f"{label}: {value}" for label, value in board_rows(board)]


def board_card(board: Board, *, repo: str, signoz_error: str) -> dict[str, Any]:
    """The Lark card. ``signoz_error`` is ``""`` when SigNoz took the record."""
    return build_feishu_report_card(
        title=f"交付日报 {board.at.astimezone(UTC8):%Y-%m-%d %H:%M} UTC+8",
        template="blue",
        kpi_fields=board_rows(board),
        sections=[
            f"SigNoz 记录：已写入。查询 `service.name = {BOARD_SERVICE_NAME}`"
            if not signoz_error
            else f"SigNoz 记录：未写入（{signoz_error}）"
        ],
        actions=[
            ("SigNoz", SIGNOZ_URL, "default"),
            (
                "Staging 发车记录",
                f"https://github.com/{repo}/actions/workflows/{TRAIN_WORKFLOW}",
                "default",
            ),
        ],
    )


def failure_card(reason: str, *, now: datetime) -> dict[str, Any]:
    """The card of a day with no board: the owner sees the gap and its reason."""
    return build_feishu_report_card(
        title=f"交付日报 {now.astimezone(UTC8):%Y-%m-%d %H:%M} UTC+8：未能计算",
        template="orange",
        kpi_fields=[("原因", reason)],
        sections=["今天没有交付数字。明天的日报会重新计算。"],
    )


def otlp_log(board: Board, *, sha: str) -> dict[str, Any]:
    """One OTLP log record that carries each measured number as an attribute."""
    sha = sha.strip().lower()
    identity = ServiceIdentity.build(
        BOARD_SERVICE_ID,
        BOARD_ENVIRONMENT,
        component="delivery-board",
        service_name=BOARD_SERVICE_NAME,
        version=sha or "unknown",
        iac_ref=sha if len(sha) == 40 else "",
    )
    resource = [
        {"key": key, "value": {"stringValue": value}}
        for key, _, value in (
            pair.partition("=")
            for pair in identity.otel_resource_attributes().split(",")
        )
    ]
    attributes = [
        {
            "key": f"delivery.{key}",
            "value": {"intValue": str(value)}
            if isinstance(value, int)
            else {"doubleValue": value},
        }
        for key, value in board.numbers().items()
        if value is not None
    ]
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": resource},
                "scopeLogs": [
                    {
                        "scope": {"name": "infra2.delivery_board"},
                        "logRecords": [
                            {
                                "timeUnixNano": str(int(board.at.timestamp() * 1e9)),
                                "severityNumber": 9,
                                "severityText": "INFO",
                                "body": {"stringValue": "infra2 delivery board"},
                                "attributes": attributes,
                            }
                        ],
                    }
                ],
            }
        ]
    }


def write_signoz(
    board: Board, env: Mapping[str, str], *, opener: Callable = open_url
) -> str:
    """Post the record to the collector. Return ``""``, or the reason of the failure."""
    url = (
        env.get("INFRA2_DELIVERY_BOARD_OTLP_LOGS_URL", "").strip()
        or DEFAULT_OTLP_LOGS_URL
    )
    try:
        _post_json(
            url,
            otlp_log(board, sha=env.get("GITHUB_SHA", "")),
            {"User-Agent": AGENT},
            opener,
        )
    except Exception as exc:  # noqa: BLE001 - the card states the failure
        return type(exc).__name__
    return ""
