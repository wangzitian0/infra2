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
        checks=(("Lint", "SUCCESS"), ("Tests", "SUCCESS"), ("Docs", "SKIPPED")),
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
        _facts(checks=(("Tests", "IN_PROGRESS"),), unresolved_threads=2), now=NOW
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


def test_no_checks_yet_is_not_green():
    verdict = gate.evaluate(_facts(checks=()), now=NOW)
    assert not verdict.ready and "no checks reported yet" in verdict.reasons


class _Gh:
    """Canned gh answers; records a merge if asked."""

    def __init__(
        self, *, unresolved=0, states=("SUCCESS",), pushed_iso="2026-09-15T06:55:22Z"
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
                    "state": "OPEN",
                    "isDraft": False,
                    "baseRefName": "main",
                    "headRefOid": "feedfacefeedface",
                    "files": [{"path": "libs/probe_specs.py"}],
                    "commits": [
                        {"committedDate": "2026-09-15T06:40:00Z"},
                        {"committedDate": self.pushed_iso},
                    ],
                }
            )
        if argv[:2] == ["pr", "checks"]:
            return json.dumps(
                [{"name": f"c{i}", "state": s} for i, s in enumerate(self.states)]
            )
        if argv[:2] == ["api", "graphql"]:
            nodes = [{"isResolved": False}] * self.unresolved + [{"isResolved": True}]
            return json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {"reviewThreads": {"nodes": nodes}}
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
    assert facts.checks == (("c0", "SUCCESS"),) and facts.unresolved_threads == 0


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
