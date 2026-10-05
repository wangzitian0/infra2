"""Tests for the Dokploy preview leak detector + remediation (infra2-owned)."""

from __future__ import annotations

import json

from tools import preview_leak_check as plc


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
    # the staging `app` compose and the unrelated project contribute nothing
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


def test_select_reaps_bare_slug_and_closed_pr_keeps_canary_and_valid() -> None:
    composes = plc.collect_preview_composes(_projects())
    orphans = plc.select_orphans(composes, open_pr_numbers={5})
    reaped = {c.compose_id for c, _ in orphans}
    # bare `main` orphan + closed pr-777 + closed legacy pr-999; branch-main, open pr-5, canary-preview, tag kept.
    assert reaped == {"mainslug", "pr777", "canary"}


def test_failsafe_keeps_prs_when_open_set_unknown_but_still_reaps_bare_slug() -> None:
    composes = plc.collect_preview_composes(_projects())
    orphans = plc.select_orphans(composes, open_pr_numbers=None)
    assert {c.compose_id for c, _ in orphans} == {"mainslug"}


def test_canary_and_valid_kinds_are_never_flagged() -> None:
    assert plc.orphan_reason("canary-preview", open_pr_numbers=set()) is None
    assert plc.orphan_reason("branch-main", open_pr_numbers=set()) is None
    assert plc.orphan_reason("tag-v1-2-3", open_pr_numbers=set()) is None
    # commit-<sha7> is a supported preview kind — must never be treated as an orphan.
    assert plc.orphan_reason("commit-1ab32d5", open_pr_numbers=set()) is None
    # closed pr-999 is reaped, open pr-999 is kept.
    assert plc.orphan_reason("pr-999", open_pr_numbers={999}) is None
    assert plc.orphan_reason("pr-999", open_pr_numbers=set()) is not None


def test_fetch_open_prs_paginates_and_failsafes() -> None:
    assert plc.fetch_open_pr_numbers(None) is None  # no token -> unknown
    got = plc.fetch_open_pr_numbers(
        "tok", opener=_opener_for([[{"number": 5}, {"number": 7}]])
    )
    assert got == {5, 7}
    assert plc.fetch_open_pr_numbers("tok", opener=_raising_opener) is None


def test_detect_reports_leaks_without_deleting() -> None:
    client = _FakeClient(_projects())
    result = plc.detect(client, token="tok", opener=_opener_for([[{"number": 5}]]))
    assert client.deleted == []  # detection never deletes
    assert {c.compose_id for c, _ in result["leaks"]} == {"mainslug", "pr777", "canary"}
    assert result["open_pr_fetch"] == "ok"


def test_remediate_deletes_confirmed_leaks_with_volumes() -> None:
    client = _FakeClient(_projects())
    leaks = plc.detect(client, token="tok", opener=_opener_for([[{"number": 5}]]))[
        "leaks"
    ]
    plc.remediate(client, leaks)
    assert sorted(cid for cid, _ in client.deleted) == ["canary", "mainslug", "pr777"]
    assert all(delete_volumes is True for _, delete_volumes in client.deleted)


def test_main_detect_exits_nonzero_on_leak_and_never_deletes(monkeypatch) -> None:
    from libs import dokploy as dokploy_module

    client = _FakeClient(_projects())
    monkeypatch.setattr(dokploy_module, "get_dokploy", lambda *a, **k: client)
    # token absent -> open-PR set unknown -> only the bare-slug leak is flagged
    # (fail-safe); still a leak, still exits non-zero, still deletes nothing.
    rc = plc.main(["--token", ""])
    assert rc == 1
    assert client.deleted == []


def test_main_remediate_deletes_and_exits_zero(monkeypatch) -> None:
    from libs import dokploy as dokploy_module

    client = _FakeClient(_projects())
    monkeypatch.setattr(dokploy_module, "get_dokploy", lambda *a, **k: client)
    rc = plc.main(["--remediate", "--token", ""])
    assert rc == 0
    # with no token only the bare-slug orphan is confirmed -> remediated
    assert client.deleted == [("mainslug", True)]


def test_detect_supports_truealpha_and_finance_report_pr_isolation() -> None:
    multi_projects = [
        {
            "name": "finance_report",
            "environments": [
                {
                    "name": "preview",
                    "compose": [
                        {
                            "name": "finance-report-preview-pr-10",
                            "composeId": "fr_pr10",
                        },
                        {
                            "name": "finance-report-preview-pr-11",
                            "composeId": "fr_pr11",
                        },
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
                        {
                            "name": "truealpha-preview-pr-20",
                            "composeId": "ta_pr20",
                        },
                        {
                            "name": "truealpha-preview-pr-99",
                            "composeId": "ta_pr99",
                        },
                    ],
                }
            ],
        },
    ]

    def multi_opener(request, timeout=0):
        url = request.full_url
        if "wangzitian0/finance_report" in url:
            return _Resp(json.dumps([{"number": 10}]).encode())
        if "wangzitian0/truealpha" in url:
            return _Resp(json.dumps([{"number": 20}]).encode())
        return _Resp(b"[]")

    client = _FakeClient(multi_projects)
    result = plc.detect(client, token="tok", opener=multi_opener)
    leaked_ids = {c.compose_id for c, _ in result["leaks"]}
    # fr_pr10 and ta_pr20 are open in their respective repos; fr_pr11 and ta_pr99 are closed leaks.
    assert leaked_ids == {"fr_pr11", "ta_pr99"}

