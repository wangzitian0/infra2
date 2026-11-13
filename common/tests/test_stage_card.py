"""The stage card: one card for the records of one stage (#1645)."""

from __future__ import annotations

import json

import pytest
from infra2_sdk.observability import CheckVerdict
from infra2_sdk.stage_record import StageRecord

import common.alerting
from common.alerting import card_body_bytes, card_budget
from common.alerting import stage_card

RUN_URL = "https://github.com/o/r/actions/runs/777"
APP_ENV = {
    "INFRA2_OUT_OF_BAND_ALERT_DELIVERY_MODE": "feishu_app",
    "INFRA2_OUT_OF_BAND_FEISHU_APP_ID": "cli_app",
    "INFRA2_OUT_OF_BAND_FEISHU_APP_SECRET": "secret",
    "INFRA2_OUT_OF_BAND_FEISHU_CHAT_ID": "oc_chat",
}


def _record(phase: str, checks: list[CheckVerdict]) -> StageRecord:
    return StageRecord(
        env="staging.release_validation",
        phase=phase,
        subject="tag:v1.2.3",
        ref="refs/tags/v1.2.3",
        event="tag",
        run_url=RUN_URL,
        started_at="2026-11-01T00:00:00Z",
        finished_at="2026-11-01T00:01:00Z",
        checks=tuple(checks),
    )


def _records(extra_passed: int = 0) -> list[StageRecord]:
    return [
        _record(
            "pre",
            [
                CheckVerdict("contract", True, seconds=0.5),
                CheckVerdict("provenance", True, seconds=1.5),
            ],
        ),
        _record("operation", [CheckVerdict("staging:platform/x", True, seconds=40.0)]),
        _record(
            "post",
            [
                CheckVerdict("app/smoke", True, seconds=3.0),
                CheckVerdict(
                    "app/staging_gate",
                    False,
                    "gate: 2 of 9 journeys failed",
                    seconds=210.0,
                ),
                CheckVerdict("infra/smoke", True, seconds=2.0),
                *(
                    CheckVerdict(f"extra{i}", True, seconds=1.0)
                    for i in range(extra_passed)
                ),
            ],
        ),
    ]


def _lines(card: dict) -> list[str]:
    return card["elements"][2]["text"]["content"].splitlines()


def test_card_lists_the_failed_check_first_with_env_tag_run_url_and_seconds() -> None:
    card = stage_card.render_stage_card(_records())
    lines = _lines(card)
    assert lines[0].startswith("❌ post/app/staging_gate")
    assert "210 s" in lines[0] and "2 of 9 journeys failed" in lines[0]
    assert [line[:1] for line in lines] == ["❌"] + ["✅"] * 5
    assert {"0.5 s", "1.5 s", "40 s", "3 s", "2 s"} <= {
        part for line in lines for part in line.split(" · ")
    }
    blob = json.dumps(card)
    assert (
        "staging.release_validation" in blob
        and "tag:v1.2.3" in blob
        and RUN_URL in blob
    )
    assert card["header"]["template"] == "red"
    assert card["config"]["update_multi"] is True
    assert card_body_bytes(card, "feishu_app") <= card_budget("feishu_app")
    assert card_body_bytes(card, "feishu_webhook") <= card_budget("feishu_webhook")


def test_card_has_at_most_twelve_check_lines_and_keeps_every_failed_check() -> None:
    card = stage_card.render_stage_card(_records(extra_passed=20))
    lines = _lines(card)
    assert len(lines) == stage_card.MAX_CHECK_LINES
    assert lines[0].startswith("❌")
    assert lines[-1].startswith("… ") and "more checks" in lines[-1]


def test_card_state_follows_the_records() -> None:
    pre, operation, post = _records()
    passed = [pre, operation, _record("post", [CheckVerdict("ok", True)])]
    states = {
        len(chunk): stage_card.render_stage_card(chunk)["header"]
        for chunk in (passed[:1], passed[:2], passed)
    }
    assert states[1]["template"] == states[2]["template"] == "blue"
    assert "Running: pre done" in states[1]["title"]["content"]
    assert states[3]["template"] == "green"
    assert states[3]["title"]["content"].endswith("Passed")


def test_card_refuses_records_of_two_stages() -> None:
    other = _record("pre", [CheckVerdict("c", True)])
    other = StageRecord.from_dict({**other.to_dict(), "subject": "tag:v9.9.9"})
    with pytest.raises(ValueError):
        stage_card.render_stage_card([_records()[0], other])


def test_app_mode_creates_the_card_then_updates_it_by_message_id(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        common.alerting,
        "deliver_feishu_app_card",
        lambda **kw: calls.append(("create", kw)) or {"data": {"message_id": "om_abc"}},
    )
    monkeypatch.setattr(
        common.alerting,
        "update_feishu_app_card",
        lambda **kw: calls.append(("update", kw)) or {},
    )
    records = _records()
    message_id = stage_card.show_stage_card(APP_ENV, records[:1])
    assert message_id == "om_abc"
    assert stage_card.show_stage_card(APP_ENV, records, message_id) == "om_abc"
    assert [kind for kind, _ in calls] == ["create", "update"]
    assert calls[1][1]["message_id"] == "om_abc"
    assert "app/staging_gate" in json.dumps(calls[1][1]["card"])
    assert "app/staging_gate" not in json.dumps(calls[0][1]["card"])


def test_webhook_mode_warns_and_sends_nothing(monkeypatch, capsys) -> None:
    def forbidden(**_kw):
        raise AssertionError("a webhook cannot update a card")

    monkeypatch.setattr(common.alerting, "deliver_feishu_app_card", forbidden)
    monkeypatch.setattr(common.alerting, "update_feishu_app_card", forbidden)
    env = {"INFRA2_OUT_OF_BAND_ALERT_DELIVERY_MODE": "feishu_webhook"}
    assert stage_card.show_stage_card(env, _records()) == ""
    assert "::warning title=Stage card::" in capsys.readouterr().err


def test_a_failing_feishu_call_warns_and_keeps_the_message_id(
    monkeypatch, capsys
) -> None:
    def broken(**_kw):
        raise OSError("down")

    monkeypatch.setattr(common.alerting, "update_feishu_app_card", broken)
    assert stage_card.show_stage_card(APP_ENV, _records(), "om_abc") == "om_abc"
    assert "::warning" in capsys.readouterr().err
