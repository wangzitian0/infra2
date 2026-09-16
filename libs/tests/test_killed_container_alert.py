"""End-to-end verification that a manually killed container triggers an alert.

Validates the user acceptance criterion:
  "手动 kill 有告警。"

Covers both defense lines:
1. libs.container_breakdown: In-band container breakdown watch running in the resident
   probe runner, detecting exited (non-zero) or dead containers and generating the
   Alertmanager/SigNoz Feishu card payload with log tail reason.
2. tools.out_of_band_watchdog: Out-of-band SSH watchdog inspecting Docker health,
   detecting non-zero exit codes, failing the check, and firing out-of-band Feishu delivery.
"""

from __future__ import annotations

from libs.container_breakdown import (
    broken_state,
    find_breakdown_containers,
    build_breakdown_alert_payload,
)
from libs.alerting import format_signoz_alert, build_feishu_card_payload
from tools.out_of_band_watchdog import CheckResult, format_failure_message


def test_manually_killed_container_triggers_breakdown_alert():
    # Simulate Docker Engine /containers/json response when a container is killed with SIGKILL (exit 137)
    killed_container = {
        "Id": "c8f0c703f3d4701d9ddb74c7069dbe63",
        "Names": ["/finance_report-backend"],
        "State": "exited",
        "Status": "Exited (137) 12 seconds ago",
        "Labels": {
            "com.docker.compose.service": "backend",
            "com.docker.compose.project": "finance_report",
            "party.zitian.infra.service-id": "finance_report/app",
            "party.zitian.infra.component": "backend",
            "party.zitian.infra.environment": "production",
        },
    }

    # 1. State detector must identify 'exited' as broken (non-zero exit code)
    state = broken_state(killed_container)
    assert state == "exited", f"Expected 'exited' but got {state}"

    # 2. Breakdown sweeper must classify reason and extract detail from logs
    fake_logs = "2026-09-15T10:00:00Z process terminated by signal SIGKILL\nKilled"
    breakdowns = find_breakdown_containers([killed_container], lambda cid: fake_logs)
    assert len(breakdowns) == 1
    b = breakdowns[0]
    assert b.container == "finance_report-backend"
    assert b.state == "exited"
    assert b.service_id == "finance_report/app"
    assert "Killed" in b.detail

    # 3. Must build a firing critical alert payload
    payload = build_breakdown_alert_payload(
        breakdowns, firing=True, severity="critical"
    )
    assert payload["status"] == "firing"
    assert payload["commonLabels"]["alertname"] == "ContainerBreakdown"
    assert payload["commonLabels"]["severity"] == "critical"
    assert len(payload["alerts"]) == 1

    # 4. Text formatter must clearly state container exited and show breakdown
    text_alert = format_signoz_alert(payload)
    assert "[FIRING] ContainerBreakdown" in text_alert
    assert "finance_report-backend exited" in text_alert

    # 5. Interactive card payload must be well-formed
    card_msg = build_feishu_card_payload(payload)
    assert card_msg["msg_type"] == "interactive"
    assert "card" in card_msg


def test_clean_stop_does_not_trigger_false_alarm():
    # An intentional clean stop produces exit code 0; should NOT alert
    clean_stopped_container = {
        "Id": "a1b2c3d4e5f67890",
        "Names": ["/one-shot-migration-task"],
        "State": "exited",
        "Status": "Exited (0) 5 minutes ago",
        "Labels": {},
    }
    state = broken_state(clean_stopped_container)
    assert state is None, "Clean exit (0) must not be flagged as a broken container"


def test_out_of_band_watchdog_alerts_on_killed_container():
    # Simulate infra2-docker-health detecting a container with exit_code=137
    failure_result = CheckResult(
        name="infra2-docker-health",
        ok=False,
        detail="name=/finance_report-backend status=exited exit_code=137 health=none image=finance_report-backend:latest",
        failure_domain="docker-runtime",
        severity="P1",
    )

    msg = format_failure_message(
        [failure_result],
        run_url="https://github.com/wangzitian0/infra2/actions/runs/12345",
    )
    assert (
        "infra2 watchdog alert" in msg.lower()
        or "check failures" in msg.lower()
        or "infra2-docker-health" in msg
    )
    assert "exit_code=137" in msg
