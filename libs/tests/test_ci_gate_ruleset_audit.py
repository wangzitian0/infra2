"""Tests for the CI-gate-inventory vs live-ruleset drift audit (#504)."""

from __future__ import annotations

import json

from tools import ci_gate_ruleset_audit as cgra

# Every audit() now also asks whether the rulesets carrying those checks are
# enforcing and un-bypassable. These tests are about the check list, so they
# state a healthy posture explicitly rather than reaching the network.
_HEALTHY_POSTURE = {"rulesets": [], "inactive": [], "bypassable": []}


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _opener_returning(rules: list[dict]):
    def opener(request, timeout=0):
        return _Resp(json.dumps(rules).encode())

    return opener


def _raising_opener(request, timeout=0):
    raise OSError("github unreachable")


def _required_status_checks_rule(contexts: list[str]) -> dict:
    return {
        "type": "required_status_checks",
        "parameters": {"required_status_checks": [{"context": c} for c in contexts]},
    }


def test_live_required_contexts_extracts_from_required_status_checks_rule() -> None:
    rules = [
        {"type": "deletion"},
        _required_status_checks_rule(["Lint Python Code", "Validate Compose Files"]),
    ]
    result = cgra._live_required_contexts(
        "wangzitian0/infra2", "main", "tok", opener=_opener_returning(rules)
    )
    # The reader now also returns the ruleset ids those rules came from, so the
    # audit can ask whether those rulesets are enforcing at all.
    contexts, ruleset_ids = result
    assert contexts == {"Lint Python Code", "Validate Compose Files"}
    assert isinstance(ruleset_ids, set)


def test_live_required_contexts_is_failsafe_none_on_network_error() -> None:
    result = cgra._live_required_contexts(
        "wangzitian0/infra2", "main", "tok", opener=_raising_opener
    )
    assert result is None


def test_audit_reports_in_sync_when_declared_matches_live(monkeypatch) -> None:
    monkeypatch.setattr(
        cgra,
        "_blocking_gates",
        lambda: [
            {"id": "infra_ci.lint_python", "workflow": "wf.yml", "job": "lint-python"}
        ],
    )
    monkeypatch.setattr(cgra, "_job_display_name", lambda *_a: "Lint Python Code")
    monkeypatch.setattr(cgra, "_defanged", lambda *_a: [])
    monkeypatch.setattr(
        cgra,
        "_live_required_contexts",
        lambda *_a, **_kw: ({"Lint Python Code"}, {1}),
    )

    monkeypatch.setattr(cgra, "_ruleset_posture", lambda *_a, **_kw: _HEALTHY_POSTURE)
    result = cgra.audit(token="tok")
    assert result["status"] == "in_sync"
    assert result["missing_from_ruleset"] == []
    assert result["extra_in_ruleset"] == []


def test_audit_flags_gate_missing_from_ruleset(monkeypatch) -> None:
    monkeypatch.setattr(
        cgra,
        "_blocking_gates",
        lambda: [
            {"id": "infra_ci.lint_python", "workflow": "wf.yml", "job": "lint-python"}
        ],
    )
    monkeypatch.setattr(cgra, "_job_display_name", lambda *_a: "Lint Python Code")
    monkeypatch.setattr(cgra, "_defanged", lambda *_a: [])
    monkeypatch.setattr(
        cgra, "_live_required_contexts", lambda *_a, **_kw: (set(), {1})
    )

    monkeypatch.setattr(cgra, "_ruleset_posture", lambda *_a, **_kw: _HEALTHY_POSTURE)
    result = cgra.audit(token="tok")
    assert result["status"] == "drift"
    assert result["missing_from_ruleset"] == ["Lint Python Code"]


def test_audit_flags_extra_check_in_ruleset_not_declared(monkeypatch) -> None:
    monkeypatch.setattr(cgra, "_blocking_gates", lambda: [])
    monkeypatch.setattr(
        cgra,
        "_live_required_contexts",
        lambda *_a, **_kw: ({"Some Unregistered Check"}, {1}),
    )

    monkeypatch.setattr(cgra, "_ruleset_posture", lambda *_a, **_kw: _HEALTHY_POSTURE)
    result = cgra.audit(token="tok")
    assert result["status"] == "drift"
    assert result["extra_in_ruleset"] == ["Some Unregistered Check"]


def test_audit_flags_self_contradicting_gate(monkeypatch) -> None:
    """A blocks_merge: true gate whose job is continue-on-error can never actually
    block — even if the ruleset happens to list it, the declaration is dishonest."""
    monkeypatch.setattr(
        cgra,
        "_blocking_gates",
        lambda: [
            {
                "id": "infra_ci.vault_policy",
                "workflow": "wf.yml",
                "job": "validate-vault-policy",
            }
        ],
    )
    monkeypatch.setattr(
        cgra, "_job_display_name", lambda *_a: "Validate Vault Policy Syntax"
    )
    monkeypatch.setattr(cgra, "_defanged", lambda *_a: ["Run ruff check"])
    monkeypatch.setattr(
        cgra,
        "_live_required_contexts",
        lambda *_a, **_kw: ({"Validate Vault Policy Syntax"}, {1}),
    )

    monkeypatch.setattr(cgra, "_ruleset_posture", lambda *_a, **_kw: _HEALTHY_POSTURE)
    result = cgra.audit(token="tok")
    assert result["status"] == "drift"
    # The report names which part was defanged, not just which gate: a
    # reader who has to open the workflow to find out is one step
    # further from fixing it.
    assert result["self_contradicting_gates"] == [
        "infra_ci.vault_policy (Run ruff check)"
    ]


def test_audit_is_undetermined_when_live_state_unreachable(monkeypatch) -> None:
    monkeypatch.setattr(cgra, "_blocking_gates", lambda: [])
    monkeypatch.setattr(cgra, "_live_required_contexts", lambda *_a, **_kw: None)

    monkeypatch.setattr(cgra, "_ruleset_posture", lambda *_a, **_kw: _HEALTHY_POSTURE)
    result = cgra.audit(token="tok")
    assert result["live_required_checks"] is None
    assert result["status"].startswith("undetermined")


def test_current_inventory_matches_the_real_infra_ci_workflow() -> None:
    """Every declared blocking gate must resolve to a real job `name:` in its
    workflow file — catches a gate pointing at a renamed/removed job."""
    gates = list(cgra._blocking_gates())
    assert len(gates) > 0, "No blocking gates found in inventory (GREEN-WHILE-EMPTY risk)"
    for gate in gates:
        name = cgra._job_display_name(gate["workflow"], gate["job"])
        assert name is not None, (
            f"{gate['id']} -> {gate['workflow']}:{gate['job']} has no job name"
        )


def test_current_inventory_has_no_self_contradicting_gates() -> None:
    """Regression guard for #504: a blocks_merge: true gate must not be
    continue-on-error (it could never actually block).

    Now reads step level too. A `continue-on-error` on the step that runs the
    check defangs the gate exactly as thoroughly as one on the job, and was
    invisible here until it was measured. A deliberate exemption declares
    itself with `# gate-exempt: <reason>` beside the step.
    """
    gates = list(cgra._blocking_gates())
    assert len(gates) > 0, "No blocking gates found in inventory (GREEN-WHILE-EMPTY risk)"
    contradicting = [
        f"{gate['id']} ({', '.join(found)})"
        for gate in gates
        if (found := cgra._defanged(gate["workflow"], gate["job"]))
    ]
    assert contradicting == []


def test_a_ruleset_that_is_not_enforcing_is_drift(monkeypatch) -> None:
    """A ruleset set to `disabled` or `evaluate` keeps every rule listed and
    stops applying them — the check list above becomes decorative.

    Editable from the GitHub UI, which leaves no commit, no diff, nothing in
    this repository for a reviewer to see. This audit is the only thing
    positioned to notice.
    """
    monkeypatch.setattr(cgra, "_blocking_gates", lambda: [])
    monkeypatch.setattr(
        cgra, "_live_required_contexts", lambda *_a, **_kw: (set(), {1})
    )
    monkeypatch.setattr(
        cgra,
        "_ruleset_posture",
        lambda *_a, **_kw: {
            "rulesets": [{"id": 1, "name": "main", "enforcement": "evaluate"}],
            "inactive": ["main: enforcement=evaluate"],
            "bypassable": [],
        },
    )

    result = cgra.audit(token="tok")
    assert result["status"] == "drift"
    assert result["inactive_rulesets"] == ["main: enforcement=evaluate"]


def test_a_bypassable_ruleset_is_drift(monkeypatch) -> None:
    """Anyone in `bypass_actors` merges past every required check.

    Nothing else in this repository can see that list — it is not a file.
    """
    monkeypatch.setattr(cgra, "_blocking_gates", lambda: [])
    monkeypatch.setattr(
        cgra, "_live_required_contexts", lambda *_a, **_kw: (set(), {1})
    )
    monkeypatch.setattr(
        cgra,
        "_ruleset_posture",
        lambda *_a, **_kw: {
            "rulesets": [{"id": 1, "name": "main", "enforcement": "active"}],
            "inactive": [],
            "bypassable": ["main: 1 bypass actor(s)"],
        },
    )

    result = cgra.audit(token="tok")
    assert result["status"] == "drift"
    assert result["bypassable_rulesets"] == ["main: 1 bypass actor(s)"]


def test_an_unreadable_posture_is_undetermined_not_in_sync(monkeypatch) -> None:
    """Fail-safe, matching how an unreachable rules API is already handled: a
    posture nobody could read is not a healthy one."""
    monkeypatch.setattr(cgra, "_blocking_gates", lambda: [])
    monkeypatch.setattr(
        cgra, "_live_required_contexts", lambda *_a, **_kw: (set(), {1})
    )
    monkeypatch.setattr(cgra, "_ruleset_posture", lambda *_a, **_kw: None)

    result = cgra.audit(token="tok")
    assert result["status"].startswith("undetermined")
    assert "in_sync" not in result["status"]


def test_live_required_contexts_returns_contexts_and_their_ruleset_ids() -> None:
    """The shape the annotation promises (#789 review found them disagreeing).

    The ids ride along with the contexts because `_ruleset_posture` needs them
    and this is the call that already knows them -- asking twice would let the
    two answers describe different rulesets.
    """
    import json
    from contextlib import contextmanager

    from tools.ci_gate_ruleset_audit import _live_required_contexts

    payload = [
        {
            "ruleset_id": 42,
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": "Lint Python Code"}]},
        },
        {"ruleset_id": 7, "type": "deletion"},
    ]

    @contextmanager
    def opener(request, timeout=None):
        assert request.headers["Authorization"] == "Bearer t0ken", (
            "the caller's token must reach the request"
        )

        class _R:
            def read(self):
                return json.dumps(payload).encode()

        yield _R()

    result = _live_required_contexts("o/r", "main", "t0ken", opener=opener)
    assert isinstance(result, tuple) and len(result) == 2, result
    contexts, ruleset_ids = result
    assert contexts == {"Lint Python Code"}
    assert ruleset_ids == {42, 7}, "every ruleset seen, not only the ones with checks"


def test_an_unreachable_rules_api_is_undetermined_not_empty() -> None:
    """None, not ([], []): a caller reading "no requirements" from a failed call
    is the same hole as an empty protected set."""
    from tools.ci_gate_ruleset_audit import _live_required_contexts

    def opener(request, timeout=None):
        raise OSError("network down")

    assert _live_required_contexts("o/r", "main", "t", opener=opener) is None
