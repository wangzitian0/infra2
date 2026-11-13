"""Tests for common/gate/replay.py (#1653)."""

from __future__ import annotations

import subprocess
from dataclasses import replace

import pytest

from common.gate.evaluator import evaluate
from common.gate import evaluator
from common.gate.replay import (
    MERGE_POLICY,
    compare,
    evaluate_in_process_of,
    normalize,
    run_replay,
)
from common.tests.test_gate_pipeline import (
    BOT,
    HEAD,
    NOW,
    OLD_HEAD,
    _green_facts,
    _verification,
)


def _in_process(mutate=lambda facts: facts):
    def run(items):
        out = []
        for facts, now in items:
            v = evaluate(mutate(facts), now=now, policy=MERGE_POLICY)
            out.append(
                {"ready": v.ready, "exit_code": v.exit_code, "reasons": list(v.reasons)}
            )
        return out

    return run


def _green(**kwargs):
    # Pushed 13 min before the merge: past the 12-minute clock.
    return _green_facts(last_push_at=NOW - 13 * 60, **kwargs)


def _items():
    ok = lambda n: normalize(_green(number=n), NOW)  # noqa: E731
    blocked = replace(_green(number=2), unresolved_threads=1, unresolved_weight=1.0)
    return [ok(1), normalize(blocked, NOW), ok(3)]


def test_looser_mutation_changes_a_verdict() -> None:
    items = _items()
    unmutated = compare(items, main=_in_process(), head=_in_process())
    assert unmutated["direction"] == "equal"
    assert unmutated["rows"] == []
    # The facts must pass without the thread, else "equal" would prove nothing.
    assert _in_process()([items[0]])[0]["ready"]
    ignores_weight = _in_process(lambda f: replace(f, unresolved_weight=0.0))
    looser = compare(items, main=_in_process(), head=ignores_weight)
    assert looser["direction"] == "looser"
    assert [r["number"] for r in looser["rows"]] == [2]
    # And the reverse is stricter.
    stricter = compare(items, main=ignores_weight, head=_in_process())
    assert stricter["direction"] == "stricter"


def test_normalize_keeps_only_what_existed_at_the_merge() -> None:
    facts = _green_facts(
        state="MERGED",
        mergeable="UNKNOWN",
        merge_state="DIRTY",
        lock_failures=None,
        comments=(("before", NOW - 5), ("after", NOW + 5)),
        reviews=(("a", HEAD, NOW - 5), ("b", HEAD, NOW + 5)),
    )
    got, now = normalize(facts, NOW)
    assert now == NOW
    assert (got.state, got.mergeable, got.merge_state) == ("OPEN", "MERGEABLE", "CLEAN")
    assert got.lock_failures == ()
    assert got.comments == (("before", NOW - 5),)
    assert [r[0] for r in got.reviews] == ["a"]


def test_a_verdict_that_gains_and_loses_reasons_is_mixed() -> None:
    items = _items()[:1]
    main = lambda _: [{"ready": False, "exit_code": 3, "reasons": ["a"]}]  # noqa: E731
    head = lambda _: [{"ready": False, "exit_code": 3, "reasons": ["b"]}]  # noqa: E731
    got = compare(items, main=main, head=head)
    assert got["direction"] == "mixed"
    assert got["rows"][0]["reasons_added"] == ["b"]


def test_replay_uses_the_settling_policy_of_the_merge(monkeypatch) -> None:
    """A bot PR whose comment names an older head waits; a `_verified_at` that
    accepts it is looser under `either` only. Under `clock` both would wait."""
    stale = replace(
        _green_facts(number=7, author=BOT),
        comments=((_verification(sha=OLD_HEAD), NOW - 61),),
    )
    items = [normalize(stale, NOW)]

    def loose(items):
        with monkeypatch.context() as m:
            m.setattr(evaluator, "_verified_at", lambda facts: NOW - 61)
            return _in_process()(items)

    got = compare(items, main=_in_process(), head=loose)
    assert got["direction"] == "looser"
    assert [r["number"] for r in got["rows"]] == [7]


def _stub_gate(root, marker):
    (root / "common" / "gate").mkdir(parents=True, exist_ok=True)
    (root / "common" / "__init__.py").write_text("")
    (root / "common" / "gate" / "__init__.py").write_text("")
    (root / "common" / "gate" / "types.py").write_text(
        "class HeadFacts:\n    def __init__(self, **kw):\n        self.__dict__.update(kw)\n"
    )
    (root / "common" / "gate" / "evaluator.py").write_text(
        "class V:\n    ready = False\n    exit_code = 3\n"
        f"    reasons = [{marker!r}]\n"
        "def evaluate(facts, now, policy):\n    return V()\n"
    )


def test_the_main_side_runs_the_gate_of_its_ref_in_its_own_tree(tmp_path) -> None:
    repo, head = tmp_path / "repo", tmp_path / "head"
    repo.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "t@e.com"),
        ("config", "user.name", "t"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    _stub_gate(repo, "main-gate")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "m"], cwd=repo, check=True)
    _stub_gate(repo, "caller-tree")  # uncommitted: not the ref
    _stub_gate(head, "head-gate")
    items = [normalize(_green(number=1), NOW)]
    got = compare(
        items,
        main=evaluate_in_process_of("main", root=repo),
        head=evaluate_in_process_of(None, root=head),
    )
    assert got["direction"] == "mixed"
    assert got["rows"][0]["reasons_removed"] == ["main-gate"]
    assert got["rows"][0]["reasons_added"] == ["head-gate"]


def test_an_empty_or_short_replay_is_never_green() -> None:
    with pytest.raises(ValueError, match="empty"):
        compare([], main=_in_process(), head=_in_process())
    with pytest.raises(ValueError):
        compare(
            _items(), main=_in_process(), head=lambda items: _in_process()(items)[:1]
        )
    with pytest.raises(ValueError, match="at least 1"):
        run_replay(0, repo="x/y", gh=lambda argv: "[]")


def test_origin_main_is_fetched_before_any_read(tmp_path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)  # no origin

    def gh(argv):
        raise AssertionError("read before the fetch of origin/main")

    with pytest.raises(subprocess.CalledProcessError):
        run_replay(1, repo="x/y", gh=gh, root=tmp_path)


def test_run_replay_judges_main_by_origin_main_and_head_by_the_checkout(
    monkeypatch, tmp_path
) -> None:
    from common.gate import replay

    calls = []

    def fake(ref, *, root):
        calls.append(ref)
        return lambda items: [
            {"ready": True, "exit_code": 0, "reasons": [str(ref)]} for _ in items
        ]

    monkeypatch.setattr(replay, "evaluate_in_process_of", fake)
    monkeypatch.setattr(replay, "fetch", lambda count, repo, gh: _items()[:1])
    monkeypatch.setattr(replay.subprocess, "run", lambda *a, **k: None)
    got = run_replay(1, repo="x/y", gh=None, root=tmp_path)
    assert calls == ["origin/main", None]
    assert got["rows"][0]["reasons_removed"] == ["origin/main"]
