"""Tests for the Dokploy preview garbage-collector (reconcile-based orphan reaper)."""

from __future__ import annotations

import json

from tools import preview_gc


class _FakeClient:
    def __init__(self, projects):
        self._projects = projects
        self.deleted: list[tuple[str, bool]] = []

    def list_projects(self):
        return self._projects

    def delete_compose(self, compose_id, *, delete_volumes=False):
        self.deleted.append((compose_id, delete_volumes))
        return {"ok": True}


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _opener_for(pages):
    state = {"i": 0}

    def opener(request, timeout=0):
        i = state["i"]
        state["i"] += 1
        batch = pages[i] if i < len(pages) else []
        return _Resp(json.dumps(batch).encode())

    return opener


def _raising_opener(request, timeout=0):
    raise OSError("github unreachable")


def _projects():
    # The current model: every preview compose lives under finance_report/preview,
    # named finance-report-preview-<alias>. `main` is the pre-rename bare orphan.
    return [
        {
            "name": "finance_report",
            "environments": [
                {"name": "staging", "compose": [{"name": "app", "composeId": "stg"}]},
                {
                    "name": "preview",
                    "compose": [
                        {
                            "name": "finance-report-preview-branch-main",
                            "composeId": "bm",
                        },
                        {
                            "name": "finance-report-preview-main",
                            "composeId": "mainslug",
                        },
                        {"name": "finance-report-preview-pr-5", "composeId": "pr5"},
                        {"name": "finance-report-preview-pr-777", "composeId": "pr777"},
                        {
                            "name": "finance-report-preview-pr-999",
                            "composeId": "canary",
                        },
                        {
                            "name": "finance-report-preview-tag-v1-2-3",
                            "composeId": "tag",
                        },
                        {
                            "name": "finance-report-preview-commit-1ab32d5",
                            "composeId": "commit",
                        },
                    ],
                },
            ],
        },
        {"name": "platform-signoz", "environments": []},  # unrelated, never touched
    ]


def test_collect_only_preview_env_composes() -> None:
    found = preview_gc.collect_preview_composes(_projects())
    by_id = {c.compose_id: c.alias for c in found}
    # the staging `app` compose and the unrelated project contribute nothing
    assert by_id == {
        "bm": "branch-main",
        "mainslug": "main",
        "pr5": "pr-5",
        "pr777": "pr-777",
        "canary": "pr-999",
        "tag": "tag-v1-2-3",
        "commit": "commit-1ab32d5",
    }


def test_select_reaps_bare_slug_and_closed_pr_keeps_canary_and_valid() -> None:
    composes = preview_gc.collect_preview_composes(_projects())
    orphans = preview_gc.select_orphans(composes, open_pr_numbers={5})
    reaped = {c.compose_id for c, _ in orphans}
    # bare `main` orphan + closed pr-777; branch-main, open pr-5, canary pr-999, tag kept.
    assert reaped == {"mainslug", "pr777"}


def test_failsafe_keeps_prs_when_open_set_unknown_but_still_reaps_bare_slug() -> None:
    composes = preview_gc.collect_preview_composes(_projects())
    orphans = preview_gc.select_orphans(composes, open_pr_numbers=None)
    assert {c.compose_id for c, _ in orphans} == {"mainslug"}


def test_canary_and_valid_kinds_are_never_reaped() -> None:
    assert preview_gc.orphan_reason("pr-999", open_pr_numbers=set()) is None
    assert preview_gc.orphan_reason("branch-main", open_pr_numbers=set()) is None
    assert preview_gc.orphan_reason("tag-v1-2-3", open_pr_numbers=set()) is None
    # commit-<sha7> is a supported preview kind — must never be treated as an orphan.
    assert preview_gc.orphan_reason("commit-1ab32d5", open_pr_numbers=set()) is None


def test_fetch_open_prs_paginates_and_failsafes() -> None:
    assert preview_gc.fetch_open_pr_numbers(None) is None  # no token -> unknown
    got = preview_gc.fetch_open_pr_numbers(
        "tok", opener=_opener_for([[{"number": 5}, {"number": 7}]])
    )
    assert got == {5, 7}
    assert preview_gc.fetch_open_pr_numbers("tok", opener=_raising_opener) is None


def test_run_dry_run_reports_without_deleting() -> None:
    client = _FakeClient(_projects())
    result = preview_gc.run(
        client, token="tok", apply=False, opener=_opener_for([[{"number": 5}]])
    )
    assert client.deleted == []  # dry-run never deletes
    assert result["would_reap"] == 2
    assert result["open_pr_fetch"] == "ok"


def test_run_apply_deletes_orphans_with_volumes() -> None:
    client = _FakeClient(_projects())
    result = preview_gc.run(
        client, token="tok", apply=True, opener=_opener_for([[{"number": 5}]])
    )
    assert sorted(cid for cid, _ in client.deleted) == ["mainslug", "pr777"]
    assert all(delete_volumes is True for _, delete_volumes in client.deleted)
    assert result["reaped"] == 2
