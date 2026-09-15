"""tools/pr_merge_gate: the session-scoped merge authority as a check."""

from __future__ import annotations

import json

import pytest

from tools import pr_merge_gate as gate

NOW = 1_800_000_000.0


def _facts(**overrides) -> gate.HeadFacts:
    base = dict(
        number=704,
        state="OPEN",
        draft=False,
        base="main",
        head_sha="abcdef0123456789",
        files=("libs/probe_specs.py",),
        last_push_at=NOW - 13 * 60,
        checks=(("Lint", "pass"), ("Tests", "pass"), ("Docs", "skipping")),
        unresolved_threads=0,
    )
    base.update(overrides)
    return gate.HeadFacts(**base)


def test_a_settled_green_head_is_mergeable():
    verdict = gate.evaluate(_facts(), now=NOW)
    assert verdict.ready and verdict.exit_code == 0 and verdict.reasons == []


def test_the_quiet_period_is_counted_from_the_last_push():
    verdict = gate.evaluate(_facts(last_push_at=NOW - 11 * 60), now=NOW)
    assert not verdict.ready and verdict.exit_code == 1
    assert verdict.quiet_remaining_seconds == 60
    assert "quiet period has 60s to run" in verdict.reasons[0]


def test_pending_checks_and_open_threads_are_not_yet_not_owner():
    verdict = gate.evaluate(
        _facts(checks=(("Tests", "pending"),), unresolved_threads=2), now=NOW
    )
    assert verdict.exit_code == 1 and not verdict.owner_required
    assert "check(s) not green: Tests" in verdict.reasons
    assert "2 review thread(s) unresolved" in verdict.reasons


def test_a_protected_file_or_a_deploy_triggering_path_needs_the_owner():
    protected = gate.evaluate(_facts(files=("AGENTS.md", "libs/x.py")), now=NOW)
    assert protected.exit_code == 2 and "AGENTS.md" in protected.reasons[0]
    runner = gate.evaluate(
        _facts(files=("bootstrap/06.iac_runner/compose.yaml",)), now=NOW
    )
    assert runner.exit_code == 2 and "trigger a deploy" in runner.reasons[0]
    workflow = gate.evaluate(_facts(files=(".github/workflows/deploy.yml",)), now=NOW)
    assert workflow.owner_required


def test_a_fractional_second_inside_the_window_is_inside_the_window():
    verdict = gate.evaluate(_facts(last_push_at=NOW - 12 * 60 + 0.4), now=NOW)
    assert not verdict.ready and verdict.quiet_remaining_seconds == 1


def test_more_threads_than_were_read_is_not_a_clean_verdict():
    verdict = gate.evaluate(_facts(review_threads_total=150), now=NOW)
    assert not verdict.ready and "only the first 100 were read" in verdict.reasons[0]


def test_state_is_the_fallback_when_gh_reports_no_bucket():
    assert gate.evaluate(_facts(checks=(("Tests", "SUCCESS"),)), now=NOW).ready
    assert not gate.evaluate(_facts(checks=(("Tests", "FAILURE"),)), now=NOW).ready


def test_event_policy_waits_for_a_review_of_the_head_then_settles_briefly():
    head = "abcdef0123456789"
    unreviewed = gate.evaluate(
        _facts(head_sha=head, last_push_at=NOW - 60), now=NOW, policy="event"
    )
    assert (
        not unreviewed.ready
        and "no automated review on head abcdef0" in unreviewed.reasons[0]
    )

    stale = _facts(
        head_sha=head,
        last_push_at=NOW - 60,
        reviews=(("copilot-pull-request-reviewer", "0000000", NOW - 600),),
    )
    assert not gate.evaluate(
        stale, now=NOW, policy="event"
    ).ready  # reviewed an older head

    fresh = _facts(
        head_sha=head,
        last_push_at=NOW - 60,
        reviews=(("copilot-pull-request-reviewer", head, NOW - 100),),
    )
    settling = gate.evaluate(fresh, now=NOW, policy="event")
    assert not settling.ready and settling.quiet_remaining_seconds == 80
    assert gate.evaluate(fresh, now=NOW + 80, policy="event").ready
    # a human review alone does not count as the automated pass the rule waits for
    human = _facts(head_sha=head, reviews=(("wangzitian0", head, NOW - 600),))
    assert not gate.evaluate(human, now=NOW, policy="event").ready
    with pytest.raises(ValueError):
        gate.evaluate(_facts(), now=NOW, policy="vibes")


def test_either_takes_the_review_early_and_keeps_the_clock_as_the_bound():
    head = "abcdef0123456789"
    reviewed = _facts(
        head_sha=head,
        last_push_at=NOW - 60,
        reviews=(("copilot-pull-request-reviewer", head, NOW - 200),),
    )
    assert gate.evaluate(reviewed, now=NOW, policy="either").ready  # 200 s after review
    unreviewed = _facts(head_sha=head, last_push_at=NOW - 11 * 60)
    pending = gate.evaluate(unreviewed, now=NOW, policy="either")
    assert not pending.ready and pending.quiet_remaining_seconds == 60
    assert (
        "no automated review" in pending.reasons[0]
        and "quiet period" in pending.reasons[0]
    )
    assert gate.evaluate(
        unreviewed, now=NOW + 60, policy="either"
    ).ready  # the clock bound


def test_the_clock_policy_ignores_reviews():
    facts = _facts(
        last_push_at=NOW - 60,
        reviews=(("copilot-pull-request-reviewer", "abcdef0123456789", NOW - 30),),
    )
    assert not gate.evaluate(facts, now=NOW).ready


def test_no_checks_yet_is_not_green():
    verdict = gate.evaluate(_facts(checks=()), now=NOW)
    assert not verdict.ready and "no checks reported yet" in verdict.reasons


class _Gh:
    """Canned gh answers; records a merge if asked."""

    def __init__(
        self, *, unresolved=0, states=("pass",), pushed_iso="2026-09-15T06:55:22Z"
    ):
        self.calls: list[list[str]] = []
        self.unresolved = unresolved
        self.states = states
        self.pushed_iso = pushed_iso

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:2] == ["pr", "view"]:
            return json.dumps(
                {
                    "number": 704,
                    "id": "PR_node",
                    "state": "OPEN",
                    "isDraft": False,
                    "baseRefName": "main",
                    "headRefOid": "feedfacefeedface",
                    "reviews": [
                        {
                            "author": {"login": "copilot-pull-request-reviewer"},
                            "commit": {"oid": "feedfacefeedface"},
                            "state": "COMMENTED",
                            "submittedAt": "2026-09-15T06:58:00Z",
                        }
                    ],
                    "files": [{"path": "libs/probe_specs.py"}],
                    "commits": [
                        {"committedDate": "2026-09-15T06:40:00Z"},
                        {"committedDate": self.pushed_iso},
                    ],
                }
            )
        if argv[:2] == ["pr", "checks"]:
            # gh's real shape (read live 2026-09-15): `state` is SUCCESS/SKIPPED/…,
            # `bucket` is pass/fail/pending/skipping; there is no `conclusion` field,
            # and `pr view --json files,commits` returns flat arrays.
            return json.dumps(
                [
                    {"name": f"c{i}", "state": "COMPLETED", "bucket": s}
                    for i, s in enumerate(self.states)
                ]
            )
        if argv[:2] == ["api", "graphql"] and "requestReviews" in argv[3]:
            self.review_requests = getattr(self, "review_requests", 0) + 1
            return json.dumps(
                {"data": {"requestReviews": {"pullRequest": {"number": 704}}}}
            )
        if argv[:2] == ["api", "graphql"]:
            nodes = [{"isResolved": False}] * self.unresolved + [{"isResolved": True}]
            return json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "totalCount": len(nodes),
                                    "nodes": nodes,
                                }
                            }
                        }
                    }
                }
            )
        if argv[:2] == ["pr", "merge"]:
            return ""
        raise AssertionError(argv)


def test_collect_reads_the_newest_commit_as_the_last_push():
    facts = gate.collect(704, gh=_Gh())
    assert facts.head_sha == "feedfacefeedface"
    assert facts.last_push_at == gate._epoch("2026-09-15T06:55:22Z")
    assert facts.checks == (("c0", "pass"),) and facts.unresolved_threads == 0
    assert facts.review_threads_total == 1
    assert facts.node_id == "PR_node"
    assert facts.reviews_on_head()[0][0] == "copilot-pull-request-reviewer"


def test_request_review_asks_copilot_only_when_the_head_is_unreviewed(capsys):
    class _Unreviewed(_Gh):
        def __call__(self, argv):
            out = super().__call__(argv)
            if list(argv)[:2] == ["pr", "view"]:
                doc = json.loads(out)
                doc["reviews"] = []
                return json.dumps(doc)
            return out

    gh = _Unreviewed()
    assert (
        gate.main(
            ["704", "--policy", "event", "--request-review"], gh=gh, now=lambda: NOW
        )
        == 1
    )
    assert gh.review_requests == 1
    assert "no automated review on head" in capsys.readouterr().out
    reviewed = _Gh()
    gate.main(
        ["704", "--policy", "event", "--request-review"], gh=reviewed, now=lambda: NOW
    )
    assert getattr(reviewed, "review_requests", 0) == 0


def test_main_merges_only_a_ready_head_and_pins_the_head_commit(capsys):
    gh = _Gh()
    ready_at = gate._epoch("2026-09-15T06:55:22Z") + 12 * 60
    assert gate.main(["704", "--merge"], gh=gh, now=lambda: ready_at - 1) == 1
    assert not any(c[:2] == ["pr", "merge"] for c in gh.calls)
    assert "quiet period has 1s to run" in capsys.readouterr().out

    assert gate.main(["704", "--merge"], gh=gh, now=lambda: ready_at) == 0
    merge = next(c for c in gh.calls if c[:2] == ["pr", "merge"])
    assert merge[merge.index("--match-head-commit") + 1] == "feedfacefeedface"
    assert "merged #704 at feedfac" in capsys.readouterr().out


def test_main_json_reports_the_owner_gate(capsys):
    class _Protected(_Gh):
        def __call__(self, argv):
            out = super().__call__(argv)
            if list(argv)[:2] == ["pr", "view"]:
                doc = json.loads(out)
                doc["files"].append({"path": "CLAUDE.md"})
                return json.dumps(doc)
            return out

    assert gate.main(["704", "--json"], gh=_Protected(), now=lambda: NOW) == 2
    doc = json.loads(capsys.readouterr().out)
    assert doc["owner_required"] and doc["ready"] is False


def test_a_failing_gh_call_is_an_error_not_a_verdict():
    def boom(argv):
        raise RuntimeError("gh pr view: not found")

    with pytest.raises(RuntimeError, match="not found"):
        gate.collect(999, gh=boom)
