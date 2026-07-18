"""Tests for tools/app_compose_id_drift.py (#524).

Uses a fake DokployClient (FakeDokploy-style, matching libs/tests/test_deploy_primitive.py)
so the check is exercised without any live Dokploy/network access.
"""

from __future__ import annotations

from libs.deploy_env_config import ComposeTarget
from tools import app_compose_id_drift as drift


class FakeDokployLookup:
    """Records find_compose_by_name calls and returns a scripted composeId."""

    def __init__(self, compose_ids: dict[tuple[str, str, str], str | None]):
        # {(compose_name, project_name, env_name): composeId-or-None-if-missing}
        self._compose_ids = compose_ids
        self.calls: list[tuple[str, str, str]] = []

    def find_compose_by_name(self, name, project_name=None, env_name=None):
        key = (name, project_name, env_name)
        self.calls.append(key)
        if key not in self._compose_ids:
            raise AssertionError(f"unscripted find_compose_by_name call: {key}")
        live_id = self._compose_ids[key]
        if live_id is None:
            return None
        return {"composeId": live_id, "name": name}


def _target(
    *,
    service="finance_report/app",
    env="staging",
    project_name="finance_report",
    compose_name="app",
    dokploy_env_name="staging",
    compose_id="A6V-hbJlgHMwgPDoTDnhH",
) -> ComposeTarget:
    return ComposeTarget(
        service=service,
        env=env,
        project_name=project_name,
        compose_name=compose_name,
        dokploy_env_name=dokploy_env_name,
        compose_id=compose_id,
    )


def test_matching_compose_id_passes_silently():
    target = _target()
    client = FakeDokployLookup(
        {("app", "finance_report", "staging"): "A6V-hbJlgHMwgPDoTDnhH"}
    )
    row = drift.check_target(client, target)
    assert row.verdict == "ok"
    assert row.live_compose_id == "A6V-hbJlgHMwgPDoTDnhH"
    assert client.calls == [("app", "finance_report", "staging")]

    rows = [row]
    assert drift.blockers(rows) == []
    report = drift.format_report(rows)
    assert "🔴" not in report  # no DRIFT/MISSING row line was emitted
    assert "every hardcoded compose_id matches" in report


def test_mismatched_compose_id_fails_naming_stale_and_correct_value():
    target = _target()
    # The live compose now has a DIFFERENT composeId than the hardcoded literal —
    # simulates the compose having been deleted and recreated in the Dokploy UI.
    client = FakeDokployLookup(
        {("app", "finance_report", "staging"): "FRESH_ID_AFTER_RECREATE"}
    )
    row = drift.check_target(client, target)
    assert row.verdict == "DRIFT"
    assert row.live_compose_id == "FRESH_ID_AFTER_RECREATE"
    # The message must name BOTH the stale literal and the correct value, so an
    # operator can fix libs/deploy_env_config.py without re-deriving anything.
    assert "A6V-hbJlgHMwgPDoTDnhH" in row.note  # the stale literal
    assert "FRESH_ID_AFTER_RECREATE" in row.note  # the correct value
    assert "finance_report/app" in row.note

    rows = [row]
    assert drift.blockers(rows) == [row]
    report = drift.format_report(rows)
    assert "DRIFT" in report
    assert "A6V-hbJlgHMwgPDoTDnhH" in report
    assert "FRESH_ID_AFTER_RECREATE" in report


def test_missing_compose_fails_with_a_clear_message():
    target = _target(
        service="truealpha/app",
        env="staging",
        project_name="truealpha",
        compose_name="app",
        dokploy_env_name="staging",
        compose_id="w4zo_fm9d2PnUY8ULzNO7",
    )
    client = FakeDokployLookup({("app", "truealpha", "staging"): None})
    row = drift.check_target(client, target)
    assert row.verdict == "missing"
    assert "truealpha" in row.note
    assert drift.blockers([row]) == [row]


def test_lookup_error_is_reported_as_error_not_drift():
    target = _target()

    class RaisingClient:
        def find_compose_by_name(self, name, project_name=None, env_name=None):
            raise RuntimeError("Dokploy API request failed: 500")

    row = drift.check_target(RaisingClient(), target)
    assert row.verdict == "error"
    assert "live lookup failed" in row.note
    assert drift.blockers([row]) == [row]
    # A bare lookup error is a CI-visible blocker (retried tomorrow) but is NOT
    # confirmed drift — it must never page Feishu on its own (#425/#475: don't alert
    # on a transient blip you can't distinguish from a real failure).
    assert drift.confirmed_drift([row]) == []


def test_confirmed_drift_includes_only_drift_and_missing():
    stale = _target(compose_id="stale-id")
    ok_row = drift.check_target(FakeDokployLookup({("app", "finance_report", "staging"): "A6V-hbJlgHMwgPDoTDnhH"}), _target())
    drift_row = drift.check_target(FakeDokployLookup({("app", "finance_report", "staging"): "new-id"}), stale)
    missing_row = drift.check_target(FakeDokployLookup({("app", "finance_report", "staging"): None}), _target())

    class RaisingClient:
        def find_compose_by_name(self, name, project_name=None, env_name=None):
            raise RuntimeError("boom")

    error_row = drift.check_target(RaisingClient(), _target())

    result = drift.confirmed_drift([ok_row, drift_row, missing_row, error_row])
    assert result == [drift_row, missing_row]


def test_scan_checks_every_registered_target():
    # scan() must not silently skip a bespoke app's compose_id — cover all of them.
    from libs.deploy_env_config import bespoke_app_compose_targets

    expected_keys = {
        (t.compose_name, t.project_name, t.dokploy_env_name)
        for t in bespoke_app_compose_targets()
    }
    client = FakeDokployLookup(
        {
            key: "SOME_ID"  # arbitrary — this test only proves every target is queried
            for key in expected_keys
        }
    )
    rows = drift.scan(client)
    assert len(rows) == len(expected_keys)
    assert set(client.calls) == expected_keys
