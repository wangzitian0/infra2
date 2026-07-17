"""Tests for the CI-gate-inventory vs live-ruleset drift audit (#504)."""

from __future__ import annotations

import json

from tools import ci_gate_ruleset_audit as cgra


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
    assert result == {"Lint Python Code", "Validate Compose Files"}


def test_live_required_contexts_is_failsafe_none_on_network_error() -> None:
    result = cgra._live_required_contexts(
        "wangzitian0/infra2", "main", "tok", opener=_raising_opener
    )
    assert result is None


def test_audit_reports_in_sync_when_declared_matches_live(monkeypatch) -> None:
    monkeypatch.setattr(
        cgra,
        "_blocking_gates",
        lambda: [{"id": "infra_ci.lint_python", "workflow": "wf.yml", "job": "lint-python"}],
    )
    monkeypatch.setattr(cgra, "_job_display_name", lambda *_a: "Lint Python Code")
    monkeypatch.setattr(cgra, "_job_continue_on_error", lambda *_a: False)
    monkeypatch.setattr(
        cgra,
        "_live_required_contexts",
        lambda *_a, **_kw: {"Lint Python Code"},
    )

    result = cgra.audit(token="tok")
    assert result["status"] == "in_sync"
    assert result["missing_from_ruleset"] == []
    assert result["extra_in_ruleset"] == []


def test_audit_flags_gate_missing_from_ruleset(monkeypatch) -> None:
    monkeypatch.setattr(
        cgra,
        "_blocking_gates",
        lambda: [{"id": "infra_ci.lint_python", "workflow": "wf.yml", "job": "lint-python"}],
    )
    monkeypatch.setattr(cgra, "_job_display_name", lambda *_a: "Lint Python Code")
    monkeypatch.setattr(cgra, "_job_continue_on_error", lambda *_a: False)
    monkeypatch.setattr(cgra, "_live_required_contexts", lambda *_a, **_kw: set())

    result = cgra.audit(token="tok")
    assert result["status"] == "drift"
    assert result["missing_from_ruleset"] == ["Lint Python Code"]


def test_audit_flags_extra_check_in_ruleset_not_declared(monkeypatch) -> None:
    monkeypatch.setattr(cgra, "_blocking_gates", lambda: [])
    monkeypatch.setattr(
        cgra, "_live_required_contexts", lambda *_a, **_kw: {"Some Unregistered Check"}
    )

    result = cgra.audit(token="tok")
    assert result["status"] == "drift"
    assert result["extra_in_ruleset"] == ["Some Unregistered Check"]


def test_audit_flags_self_contradicting_gate(monkeypatch) -> None:
    """A blocks_merge: true gate whose job is continue-on-error can never actually
    block — even if the ruleset happens to list it, the declaration is dishonest."""
    monkeypatch.setattr(
        cgra,
        "_blocking_gates",
        lambda: [{"id": "infra_ci.vault_policy", "workflow": "wf.yml", "job": "validate-vault-policy"}],
    )
    monkeypatch.setattr(cgra, "_job_display_name", lambda *_a: "Validate Vault Policy Syntax")
    monkeypatch.setattr(cgra, "_job_continue_on_error", lambda *_a: True)
    monkeypatch.setattr(
        cgra, "_live_required_contexts", lambda *_a, **_kw: {"Validate Vault Policy Syntax"}
    )

    result = cgra.audit(token="tok")
    assert result["status"] == "drift"
    assert result["self_contradicting_gates"] == ["infra_ci.vault_policy"]


def test_audit_is_undetermined_when_live_state_unreachable(monkeypatch) -> None:
    monkeypatch.setattr(cgra, "_blocking_gates", lambda: [])
    monkeypatch.setattr(cgra, "_live_required_contexts", lambda *_a, **_kw: None)

    result = cgra.audit(token="tok")
    assert result["live_required_checks"] is None
    assert result["status"].startswith("undetermined")


def test_current_inventory_matches_the_real_infra_ci_workflow() -> None:
    """Every declared blocking gate must resolve to a real job `name:` in its
    workflow file — catches a gate pointing at a renamed/removed job."""
    for gate in cgra._blocking_gates():
        name = cgra._job_display_name(gate["workflow"], gate["job"])
        assert name is not None, f"{gate['id']} -> {gate['workflow']}:{gate['job']} has no job name"


def test_current_inventory_has_no_self_contradicting_gates() -> None:
    """Regression guard for #504: a blocks_merge: true gate must not be
    continue-on-error (it could never actually block)."""
    contradicting = [
        gate["id"]
        for gate in cgra._blocking_gates()
        if cgra._job_continue_on_error(gate["workflow"], gate["job"])
    ]
    assert contradicting == []
