"""Tests for the Dokploy preview leak detector + remediation (infra2-owned)."""

from __future__ import annotations

import json

from common.deploy import preview_leak_check as plc


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
                            "name": "finance-report-preview-canary-preview",
                            "composeId": "canary_slot",
                        },
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
    found = plc.collect_preview_composes(_projects())
    by_id = {c.compose_id: c.alias for c in found}
    # Only prefixed preview composes or valid-kind bare aliases are collected;
    # the staging `app` compose and the unrelated project contribute nothing.
    assert by_id == {
        "bm": "branch-main",
        "mainslug": "main",
        "pr5": "pr-5",
        "pr777": "pr-777",
        "canary_slot": "canary-preview",
        "canary": "pr-999",
        "tag": "tag-v1-2-3",
        "commit": "commit-1ab32d5",
    }


def test_select_reaps_invalid_prefix_closed_pr_and_deleted_branch() -> None:
    composes = plc.collect_preview_composes(_projects())
    orphans = plc.select_orphans(
        composes,
        open_pr_numbers={5},
        active_branches={"main"},
    )
    reaped = {c.compose_id for c, _ in orphans}
    # bare `main`, retired `canary-preview`/`tag-*`/`commit-*`, closed pr-777, closed pr-999 are reaped;
    # active branch-main and open pr-5 are kept.
    assert reaped == {"mainslug", "pr777", "canary", "canary_slot", "tag", "commit"}


def test_select_reaps_deleted_remote_branch() -> None:
    composes = plc.collect_preview_composes(_projects())
    orphans = plc.select_orphans(
        composes,
        open_pr_numbers={5, 777, 999},
        active_branches={"feat/other"},
    )
    reaped = {c.compose_id for c, _ in orphans}
    assert "bm" in reaped


def test_failsafe_keeps_prs_and_branches_when_sets_unknown_but_reaps_invalid_prefixes() -> (
    None
):
    composes = plc.collect_preview_composes(_projects())
    orphans = plc.select_orphans(composes, open_pr_numbers=None, active_branches=None)
    assert {c.compose_id for c, _ in orphans} == {
        "mainslug",
        "canary_slot",
        "tag",
        "commit",
    }


def test_orphan_reason_for_retired_and_active_kinds() -> None:
    assert plc.ALWAYS_KEEP_ALIASES == frozenset()
    assert plc.orphan_reason("canary-preview", open_pr_numbers=set()) is not None
    assert plc.orphan_reason("tag-v1-2-3", open_pr_numbers=set()) is not None
    assert plc.orphan_reason("commit-1ab32d5", open_pr_numbers=set()) is not None
    assert (
        plc.orphan_reason(
            "branch-main", open_pr_numbers=set(), active_branches={"main"}
        )
        is None
    )
    assert (
        plc.orphan_reason("branch-main", open_pr_numbers=set(), active_branches={"dev"})
        == "deleted-branch preview (branch-main)"
    )
    assert plc.orphan_reason("pr-999", open_pr_numbers={999}) is None
    assert plc.orphan_reason("pr-999", open_pr_numbers=set()) is not None


def test_fetch_open_prs_and_branches_paginate_and_failsafe() -> None:
    assert plc.fetch_open_pr_numbers(None) is None
    assert plc.fetch_remote_branches(None) is None
    got = plc.fetch_open_pr_numbers(
        "tok", opener=_opener_for([[{"number": 5}, {"number": 7}]])
    )
    assert got == {5, 7}
    got_branches = plc.fetch_remote_branches(
        "tok", opener=_opener_for([[{"name": "main"}, {"name": "feat/x"}]])
    )
    assert got_branches == {"main", "feat/x"}
    assert plc.fetch_open_pr_numbers("tok", opener=_raising_opener) is None
    assert plc.fetch_remote_branches("tok", opener=_raising_opener) is None


def test_detect_reports_leaks_without_deleting() -> None:
    def opener(request, timeout=0):
        url = request.full_url
        if "/pulls" in url:
            return _Resp(json.dumps([{"number": 5}]).encode())
        if "/branches" in url:
            return _Resp(json.dumps([{"name": "main"}]).encode())
        return _Resp(b"[]")

    client = _FakeClient(_projects())
    result = plc.detect(client, token="tok", opener=opener)
    assert client.deleted == []  # detection never deletes
    assert {c.compose_id for c, _ in result["leaks"]} == {
        "mainslug",
        "pr777",
        "canary",
        "canary_slot",
        "tag",
        "commit",
    }
    assert result["open_pr_fetch"] == "ok"


def test_remediate_deletes_confirmed_leaks_with_volumes() -> None:
    def opener(request, timeout=0):
        url = request.full_url
        if "/pulls" in url:
            return _Resp(json.dumps([{"number": 5}]).encode())
        if "/branches" in url:
            return _Resp(json.dumps([{"name": "main"}]).encode())
        return _Resp(b"[]")

    client = _FakeClient(_projects())
    leaks = plc.detect(client, token="tok", opener=opener)["leaks"]
    plc.remediate(client, leaks)
    assert sorted(cid for cid, _ in client.deleted) == [
        "canary",
        "canary_slot",
        "commit",
        "mainslug",
        "pr777",
        "tag",
    ]
    assert all(delete_volumes is True for _, delete_volumes in client.deleted)


def test_main_detect_exits_nonzero_on_leak_and_never_deletes(monkeypatch) -> None:
    from common.deploy import dokploy_client as dokploy_module

    client = _FakeClient(_projects())
    monkeypatch.setattr(dokploy_module, "get_dokploy", lambda *a, **k: client)
    rc = plc.main(["--token", ""])
    assert rc == 1
    assert client.deleted == []


def test_main_remediate_deletes_and_exits_zero(monkeypatch) -> None:
    from common.deploy import dokploy_client as dokploy_module

    client = _FakeClient(_projects())
    monkeypatch.setattr(dokploy_module, "get_dokploy", lambda *a, **k: client)
    rc = plc.main(["--remediate", "--token", ""])
    assert rc == 0
    assert sorted(cid for cid, _ in client.deleted) == [
        "canary_slot",
        "commit",
        "mainslug",
        "tag",
    ]


def test_multi_project_leak_check_isolates_repos() -> None:
    projects = [
        {
            "name": "finance_report",
            "environments": [
                {
                    "name": "preview",
                    "compose": [
                        {"name": "finance-report-preview-pr-10", "composeId": "fr_10"},
                    ],
                }
            ],
        },
        {
            "name": "truealpha",
            "environments": [
                {
                    "name": "preview",
                    "compose": [
                        {"name": "truealpha-preview-pr-20", "composeId": "ta_20"},
                        {"name": "truealpha-preview-pr-30", "composeId": "ta_30"},
                    ],
                }
            ],
        },
    ]

    def opener(request, timeout=0):
        url = request.full_url
        if "finance_report" in url:
            # PR 10 open in finance_report, PR 20 is NOT
            batch = [{"number": 10}]
        elif "truealpha" in url:
            # PR 20 open in truealpha, PR 30 closed
            batch = [{"number": 20}]
        else:
            batch = []
        return _Resp(json.dumps(batch).encode())

    client = _FakeClient(projects)
    result = plc.detect(client, token="tok", opener=opener)
    leak_ids = {c.compose_id for c, _ in result["leaks"]}
    # ta_20 is kept (open in truealpha, even though not in finance_report)
    # fr_10 is kept (open in finance_report)
    # ta_30 is leaked (closed in truealpha)
    assert leak_ids == {"ta_30"}
    assert result["open_pr_fetch"] == "ok"


def test_detect_legacy_pr_compose_in_test_env_as_closed_pr() -> None:
    """Detect closed PR bare compose in test environment while ignoring non-PR composes."""
    projects = [
        {
            "name": "finance_report",
            "environments": [
                {
                    "name": "test",
                    "compose": [
                        {"name": "app", "composeId": "test_app"},
                        {"name": "e2e-runner", "composeId": "test_e2e"},
                        {"name": "pr-2656", "composeId": "DSJ7jnbs8zz39s545ahmL"},
                    ],
                },
                {
                    "name": "preview",
                    "compose": [
                        {"name": "pr-2657", "composeId": "bare_preview_pr"},
                    ],
                },
            ],
        }
    ]
    composes = plc.collect_preview_composes(projects)
    assert {(c.compose_id, c.alias) for c in composes} == {
        ("DSJ7jnbs8zz39s545ahmL", "pr-2656"),
        ("bare_preview_pr", "pr-2657"),
    }

    client = _FakeClient(projects)
    result = plc.detect(
        client,
        token="tok",
        opener=lambda req, timeout=0: _Resp(
            json.dumps([{"number": 2657}] if "/pulls" in req.full_url else []).encode()
        ),
    )
    assert [(c.compose_id, reason) for c, reason in result["leaks"]] == [
        ("DSJ7jnbs8zz39s545ahmL", "closed-PR preview (pr-2656)")
    ]
