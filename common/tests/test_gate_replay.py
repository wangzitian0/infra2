"""Tests for common/gate/replay.py (#1653)."""

from __future__ import annotations

from dataclasses import replace

from common.gate.evaluator import evaluate
from common.gate.replay import compare, normalize
from common.tests.test_gate_pipeline import HEAD, NOW, _green_facts


def _in_process(mutate=lambda facts: facts):
    def run(items):
        out = []
        for facts, now in items:
            v = evaluate(mutate(facts), now=now)
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
