"""Contract for the production release card (common/alerting/release_card.py).

The tests replace the Feishu HTTP layer. Each test reads what the code sent: the method,
the path and the body of every request."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from urllib.error import URLError

import pytest

import common.alerting as alerting
from common.alerting import release_card as rc

REQUEST = {
    "service": "truealpha/app",
    "deploy_type": "prod",
    "version_ref": "v0.2.55",
    "source_sha": "71b08493188ba245abda64a5bf945e443a9360ce",
}
RUN_URL = "https://github.com/wangzitian0/infra2/actions/runs/39947166819"
APP_ENV = {
    "INFRA2_OUT_OF_BAND_ALERT_DELIVERY_MODE": "feishu_app",
    "INFRA2_OUT_OF_BAND_FEISHU_APP_ID": "cli_test",
    "INFRA2_OUT_OF_BAND_FEISHU_APP_SECRET": "app-credential",
    "INFRA2_OUT_OF_BAND_FEISHU_CHAT_ID": "oc_test",
    "APP_DEPLOY_REQUEST_JSON": json.dumps(REQUEST),
    "GITHUB_REPOSITORY": "wangzitian0/infra2",
    "GITHUB_RUN_ID": "39947166819",
}
WEBHOOK_ENV = {
    "INFRA2_OUT_OF_BAND_ALERT_DELIVERY_MODE": "feishu_webhook",
    "INFRA2_OUT_OF_BAND_FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/abc",
    "APP_DEPLOY_REQUEST_JSON": json.dumps(REQUEST),
    "GITHUB_REPOSITORY": "wangzitian0/infra2",
    "GITHUB_RUN_ID": "39947166819",
}


class FakeFeishu:
    """A Feishu that records each request and answers like the OpenAPI."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, request, timeout):
        body = json.loads(request.data.decode("utf-8")) if request.data else {}
        self.calls.append(
            {
                "method": request.get_method(),
                "url": request.full_url,
                "authorization": request.headers.get("Authorization", ""),
                "body": body,
            }
        )
        url = request.full_url
        if url.endswith("/tenant_access_token/internal"):
            payload = {"code": 0, "tenant_access_token": "tenant-token"}
        elif url.endswith("/reply"):
            payload = {"code": 0, "data": {"message_id": "om_reply"}}
        elif "receive_id_type=chat_id" in url:
            payload = {"code": 0, "data": {"message_id": "om_card"}}
        else:
            payload = {"code": 0}
        return _Response(payload)

    def sends(self) -> list[dict]:
        """The calls that are not token requests."""
        return [c for c in self.calls if "tenant_access_token" not in c["url"]]


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


@pytest.fixture
def feishu(monkeypatch) -> FakeFeishu:
    fake = FakeFeishu()
    monkeypatch.setattr("common.alerting.urlopen", fake)
    return fake


def _card(stage: str, **overrides) -> rc.ReleaseCard:
    values = {
        "stage": stage,
        "service": REQUEST["service"],
        "environment": "prod",
        "version_ref": REQUEST["version_ref"],
        "source_sha": REQUEST["source_sha"],
        "run_url": RUN_URL,
    }
    values.update(overrides)
    return rc.ReleaseCard(**values)


@pytest.mark.parametrize("stage", sorted(rc.STAGES))
def test_every_stage_renders_a_shared_card_with_its_colour_and_label(
    stage: str,
) -> None:
    template, label = rc.STAGES[stage]

    card = rc.render_release_card(_card(stage))

    assert card["config"]["update_multi"] is True
    assert card["header"]["template"] == template
    assert label in card["header"]["title"]["content"]
    assert "truealpha/app prod v0.2.55" in card["header"]["title"]["content"]
    buttons = [e for e in card["elements"] if e["tag"] == "action"][0]["actions"]
    assert buttons[0]["url"] == RUN_URL


def test_an_unknown_stage_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown release stage"):
        _card("approved-ish")


def test_the_approval_card_says_when_the_request_expires() -> None:
    card = rc.render_release_card(
        _card("awaiting_approval", approval_expires_at="11:59 UTC")
    )

    text = json.dumps(card)
    assert "before 11:59 UTC" in text
    assert "has no effect" in text


def test_a_secret_in_the_detail_never_reaches_the_card() -> None:
    card = rc.render_release_card(
        _card("failed", detail="deploy_v2 failed: password=hunter2 refused")
    )

    assert "hunter2" not in json.dumps(card)


def test_open_creates_the_card_in_the_chat_and_returns_its_id(
    feishu: FakeFeishu,
) -> None:
    message_id = rc.open_release_card(APP_ENV, _card("requested"))

    assert message_id == "om_card"
    send = feishu.sends()[0]
    assert send["method"] == "POST"
    assert "/open-apis/im/v1/messages?receive_id_type=chat_id" in send["url"]
    assert send["authorization"] == "Bearer tenant-token"
    assert send["body"]["receive_id"] == "oc_test"
    assert send["body"]["msg_type"] == "interactive"
    assert json.loads(send["body"]["content"])["config"]["update_multi"] is True


def test_advance_replaces_the_same_message_and_does_not_create_another(
    feishu: FakeFeishu,
) -> None:
    done = rc.advance_release_card(APP_ENV, "om_card", _card("deploying"))

    assert done is True
    send = feishu.sends()
    assert len(send) == 1
    assert send[0]["method"] == "PATCH"
    assert send[0]["url"].endswith("/open-apis/im/v1/messages/om_card")
    content = json.loads(send[0]["body"]["content"])
    assert "Deploying" in content["header"]["title"]["content"]
    assert content["config"]["update_multi"] is True


def test_a_reply_goes_into_the_thread_of_the_card(feishu: FakeFeishu) -> None:
    sent = rc.reply_release_failure(APP_ENV, "om_card", "llm is unhealthy")

    assert sent is True
    send = feishu.sends()[0]
    assert send["url"].endswith("/open-apis/im/v1/messages/om_card/reply")
    assert send["body"]["reply_in_thread"] is True
    assert json.loads(send["body"]["content"]) == {"text": "llm is unhealthy"}


def test_a_webhook_cannot_update_a_card_so_only_a_person_stage_sends_text(
    feishu: FakeFeishu,
) -> None:
    assert rc.open_release_card(WEBHOOK_ENV, _card("requested")) == ""
    assert rc.advance_release_card(WEBHOOK_ENV, "", _card("deploying")) is False
    assert feishu.calls == []

    assert rc.advance_release_card(WEBHOOK_ENV, "", _card("awaiting_approval")) is True

    assert len(feishu.calls) == 1
    text = feishu.calls[0]["body"]["content"]["text"]
    assert "Waiting for approval" in text
    assert RUN_URL in text


@pytest.mark.parametrize("env", [APP_ENV, WEBHOOK_ENV])
def test_a_failing_feishu_never_raises(monkeypatch, env: dict) -> None:
    def broken(_request, timeout):
        raise URLError("network down")

    monkeypatch.setattr("common.alerting.urlopen", broken)

    assert rc.open_release_card(env, _card("awaiting_approval")) == ""
    assert rc.advance_release_card(env, "om_card", _card("failed")) is False
    assert rc.reply_release_failure(env, "om_card", "reason") is False
    rc.note_progress({**env, rc.MESSAGE_ID_ENV: "om_card"}, "sync", "done")
    assert rc.note_rollback({**env, rc.MESSAGE_ID_ENV: "om_card"}, "reason") is False


def test_progress_changes_the_card_only_when_the_run_has_one(
    feishu: FakeFeishu,
) -> None:
    rc.note_progress(APP_ENV, "secret supply for truealpha/app in staging", "completed")
    assert feishu.calls == []

    rc.note_progress(
        {**APP_ENV, rc.MESSAGE_ID_ENV: "om_card"},
        "secret supply for truealpha/app in staging",
        "completed",
    )

    content = json.loads(feishu.sends()[0]["body"]["content"])
    assert "secret supply for truealpha/app in staging: completed" in json.dumps(
        content
    )


def test_a_rollback_marks_the_card_and_replies_with_the_reason(
    feishu: FakeFeishu,
) -> None:
    handled = rc.note_rollback(
        {**APP_ENV, rc.MESSAGE_ID_ENV: "om_card"},
        "[WARN] Deploy rollback event\n- Reason: llm is unhealthy",
    )

    assert handled is True
    methods = [(c["method"], c["url"].rsplit("/", 1)[-1]) for c in feishu.sends()]
    assert methods == [("PATCH", "om_card"), ("POST", "reply")]
    patched = json.loads(feishu.sends()[0]["body"]["content"])
    assert "Rolled back" in patched["header"]["title"]["content"]


def test_a_rollback_of_a_run_without_a_card_is_left_to_the_text_path(
    feishu: FakeFeishu,
) -> None:
    assert rc.note_rollback(APP_ENV, "reason") is False
    assert feishu.calls == []


def test_the_cli_writes_the_message_id_to_the_step_output(
    feishu: FakeFeishu, tmp_path: Path
) -> None:
    output = tmp_path / "github_output"

    code = rc.main(
        ["open", "--stage", "requested", "--github-output", str(output)], env=APP_ENV
    )

    assert code == 0
    assert output.read_text(encoding="utf-8") == "message_id=om_card\n"


def test_the_cli_exits_zero_when_feishu_is_down(monkeypatch, tmp_path: Path) -> None:
    def broken(_request, timeout):
        raise URLError("network down")

    monkeypatch.setattr("common.alerting.urlopen", broken)
    output = tmp_path / "github_output"

    code = rc.main(
        ["open", "--stage", "requested", "--github-output", str(output)], env=APP_ENV
    )

    assert code == 0
    assert output.read_text(encoding="utf-8") == "message_id=\n"


def test_the_out_of_band_names_win_over_the_generic_names() -> None:
    config = alerting.out_of_band_app_config(
        {
            "INFRA2_OUT_OF_BAND_ALERT_DELIVERY_MODE": "feishu_app",
            "ALERT_DELIVERY_MODE": "feishu_webhook",
            "INFRA2_OUT_OF_BAND_FEISHU_APP_ID": "specific",
            "FEISHU_APP_ID": "generic",
            "FEISHU_CHAT_ID": "oc_generic",
        }
    )

    assert config["mode"] == "feishu_app"
    assert config["app_id"] == "specific"
    assert config["chat_id"] == "oc_generic"
    assert config["api_base"] == "https://open.feishu.cn"
    assert alerting.out_of_band_app_config({})["mode"] == "feishu_webhook"


def test_the_message_id_is_read_from_the_response_or_is_empty() -> None:
    assert alerting.message_id_of({"data": {"message_id": "om_1"}}) == "om_1"
    assert alerting.message_id_of({"data": {}}) == ""
    assert alerting.message_id_of({}) == ""


def test_a_rollback_counts_as_shown_when_only_the_reply_goes_out(monkeypatch) -> None:
    fake = FakeFeishu()

    def patch_fails(request, timeout):
        if request.get_method() == "PATCH":
            raise URLError("update refused")
        return fake(request, timeout)

    monkeypatch.setattr("common.alerting.urlopen", patch_fails)

    handled = rc.note_rollback({**APP_ENV, rc.MESSAGE_ID_ENV: "om_card"}, "reason")

    assert handled is True
    assert [c["url"].rsplit("/", 1)[-1] for c in fake.sends()] == ["reply"]


def test_the_expiry_is_the_run_creation_time_plus_the_request_lifetime() -> None:
    def fake_run(command, **_kwargs):
        assert command[:2] == ["gh", "api"]
        assert command[2] == "repos/wangzitian0/infra2/actions/runs/39947166819"
        return subprocess.CompletedProcess(command, 0, "2026-10-23T11:35:39Z\n", "")

    assert rc.expiry_text(APP_ENV, 1440, run=fake_run) == "11:59 UTC"


def test_an_unreadable_run_time_gives_no_expiry_and_no_error() -> None:
    def broken_run(command, **_kwargs):
        raise subprocess.CalledProcessError(1, command)

    assert rc.expiry_text(APP_ENV, 1440, run=broken_run) == ""


def test_a_rollback_is_remembered_for_the_later_steps_of_the_job(
    feishu: FakeFeishu, tmp_path: Path
) -> None:
    github_env = tmp_path / "github_env"
    env = {**APP_ENV, rc.MESSAGE_ID_ENV: "om_card", "GITHUB_ENV": str(github_env)}

    assert rc.note_rollback(env, "reason") is True

    assert github_env.read_text(encoding="utf-8") == "RELEASE_CARD_STAGE=rolled_back\n"


def test_the_failed_step_does_not_overwrite_a_card_that_a_rollback_marked(
    feishu: FakeFeishu,
) -> None:
    env = {**APP_ENV, rc.MESSAGE_ID_ENV: "om_card", rc.STAGE_ENV: "rolled_back"}

    assert rc.main(["advance", "--stage", "failed"], env=env) == 0
    assert feishu.calls == []

    env.pop(rc.STAGE_ENV)
    assert rc.main(["advance", "--stage", "failed"], env=env) == 0
    assert feishu.sends()[0]["method"] == "PATCH"


def test_the_deploy_rollback_notice_uses_the_card_and_not_the_text(
    feishu: FakeFeishu, monkeypatch
) -> None:
    from common.deploy import promote

    for key, value in {**APP_ENV, rc.MESSAGE_ID_ENV: "om_card"}.items():
        monkeypatch.setenv(key, value)

    promote._notify_rollback_event(
        {
            "service": "truealpha/app",
            "environment": "prod",
            "reason": "llm is unhealthy",
            "from_version": "v0.2.56",
            "to_version": "v0.2.55",
            "action": "rolled_back",
            "manual_intervention_required": False,
        }
    )

    kinds = [(c["method"], c["url"].rsplit("/", 1)[-1]) for c in feishu.sends()]
    assert kinds == [("PATCH", "om_card"), ("POST", "reply")]


def test_the_deploy_rollback_notice_sends_text_when_the_run_has_no_card(
    feishu: FakeFeishu, monkeypatch
) -> None:
    from common.deploy import promote

    for key, value in APP_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv(rc.MESSAGE_ID_ENV, raising=False)

    promote._notify_rollback_event(
        {
            "service": "truealpha/app",
            "environment": "staging",
            "reason": "llm is unhealthy",
            "from_version": "v0.2.56",
            "to_version": "v0.2.55",
            "action": "rolled_back",
            "manual_intervention_required": False,
        }
    )

    sent = feishu.sends()
    assert len(sent) == 1
    assert sent[0]["url"].endswith("receive_id_type=chat_id")
    assert "llm is unhealthy" in sent[0]["body"]["content"]


def test_each_finished_deploy_phase_reaches_the_card_of_the_run(
    feishu: FakeFeishu, monkeypatch
) -> None:
    from common.deploy import v2

    for key, value in APP_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv(rc.MESSAGE_ID_ENV, raising=False)
    v2._progress(
        "secret supply for truealpha/app in staging", "completed", time.monotonic()
    )
    assert feishu.calls == []

    monkeypatch.setenv(rc.MESSAGE_ID_ENV, "om_card")
    v2._progress(
        "secret supply for truealpha/app in staging", "completed", time.monotonic()
    )

    assert feishu.sends()[0]["method"] == "PATCH"
