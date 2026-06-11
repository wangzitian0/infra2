"""Tests for container-breakdown detection + alerting (the 'why is it down' gap)."""

from __future__ import annotations

from libs.container_breakdown import (
    Breakdown,
    broken_state,
    build_breakdown_alert_payload,
    classify_reason,
    container_name,
    find_breakdown_containers,
)


def test_broken_state_flags_restarting_and_unhealthy_only():
    assert (
        broken_state({"State": "restarting", "Status": "Restarting (1) 3s ago"})
        == "restarting"
    )
    assert (
        broken_state({"State": "running", "Status": "Up 2 hours (unhealthy)"})
        == "unhealthy"
    )
    assert broken_state({"State": "running", "Status": "Up 2 hours (healthy)"}) is None
    assert broken_state({"State": "exited", "Status": "Exited (0) 1h ago"}) is None


def test_container_name_strips_leading_slash():
    assert (
        container_name({"Names": ["/finance_report-app-vault-agent"]})
        == "finance_report-app-vault-agent"
    )
    assert container_name({"Id": "abcdef1234567890"}) == "abcdef123456"


def test_classify_reason_matches_known_breakdown_first():
    logs = "starting agent\nVAULT_ROLE_ID and VAULT_SECRET_ID are required\nexiting"
    reason, detail = classify_reason(logs)
    assert "Vault AppRole creds missing" in reason
    assert "VAULT_ROLE_ID" in detail


def test_classify_reason_falls_back_to_last_nonempty_line():
    reason, detail = classify_reason("boom: something unexpected\n\n")
    assert "see log tail" in reason
    assert detail == "boom: something unexpected"


def test_classify_reason_handles_no_logs():
    reason, detail = classify_reason("")
    assert "no logs" in reason
    assert detail == ""


def test_find_breakdown_containers_filters_and_attaches_reason():
    containers = [
        {
            "Id": "1",
            "Names": ["/healthy-svc"],
            "State": "running",
            "Status": "Up (healthy)",
        },
        {
            "Id": "2",
            "Names": ["/vault-agent"],
            "State": "restarting",
            "Status": "Restarting (1)",
        },
        {"Id": "3", "Names": ["/sick"], "State": "running", "Status": "Up (unhealthy)"},
    ]
    logs = {
        "2": "VAULT_ROLE_ID and VAULT_SECRET_ID are required",
        "3": "connection refused",
    }
    found = find_breakdown_containers(containers, lambda cid: logs.get(cid, ""))

    names = {b.container for b in found}
    assert names == {"vault-agent", "sick"}  # healthy one excluded
    by_name = {b.container: b for b in found}
    assert by_name["vault-agent"].state == "restarting"
    assert "Vault AppRole creds missing" in by_name["vault-agent"].reason
    assert "dependency unreachable" in by_name["sick"].reason


def test_build_alert_payload_shape_is_alertmanager_like():
    bd = Breakdown(
        container="vault-agent",
        state="restarting",
        reason="Vault creds missing",
        detail="...required",
    )
    payload = build_breakdown_alert_payload([bd])

    assert payload["status"] == "firing"
    assert payload["commonLabels"]["alertname"] == "ContainerBreakdown"
    assert payload["commonLabels"]["severity"] == "critical"
    assert len(payload["alerts"]) == 1
    alert = payload["alerts"][0]
    assert alert["labels"]["service"] == "vault-agent"
    assert alert["labels"]["failure_domain"] == "runtime"
    assert "vault-agent restarting" in alert["annotations"]["summary"]


def test_build_alert_payload_resolved_when_empty():
    payload = build_breakdown_alert_payload([], firing=False)
    assert payload["status"] == "resolved"
    assert payload["alerts"] == []


def test_run_once_respects_renotify_window(monkeypatch):
    import tools.container_breakdown_watch as w

    breakdown = Breakdown(
        container="vault-agent", state="restarting", reason="r", detail="d"
    )
    monkeypatch.setattr(w, "sweep", lambda client, tail: [breakdown])
    posted: list = []
    monkeypatch.setattr(w, "_post_alert", lambda payload: posted.append(payload))

    last: dict = {}
    # first sweep alerts
    assert w.run_once(client=None, log_tail=25, last_alerted=last, renotify=1800) == 1
    # second sweep within window is suppressed
    assert w.run_once(client=None, log_tail=25, last_alerted=last, renotify=1800) == 0
    assert len(posted) == 1
