"""Contract for the production release card (common/alerting/release_card.py).

The tests replace the Feishu HTTP layer. Each test reads what the code sent: the method,
the path and the body of every request."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
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
EVENT = {
    "service": "truealpha/app",
    "environment": "prod",
    "reason": "llm is unhealthy",
    "from_version": "v0.2.56",
    "to_version": "v0.2.55",
    "action": "rolled_back",
    "manual_intervention_required": False,
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


@pytest.fixture(autouse=True)
def _no_abandoned_threads():
    rc._ABANDONED.clear()
    yield
    rc._ABANDONED.clear()


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
    assert "this card is stale" in text

    without_time = json.dumps(rc.render_release_card(_card("awaiting_approval")))
    assert "When that time has passed" not in without_time
    assert "this card is stale" in without_time


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


def test_the_webhook_mode_wins_even_when_app_credentials_are_present(
    feishu: FakeFeishu,
) -> None:
    """The mode selects the path. A webhook run that also holds app credentials still
    opens no card, so a stray credential cannot start a card the run cannot update."""
    env = {**APP_ENV, "INFRA2_OUT_OF_BAND_ALERT_DELIVERY_MODE": "feishu_webhook"}

    assert rc.open_release_card(env, _card("requested")) == ""
    assert rc.advance_release_card(env, "om_card", _card("deploying")) is False
    assert rc.reply_release_failure(env, "om_card", "reason") is False

    assert feishu.calls == []


def test_a_webhook_cannot_update_a_card_so_no_stage_sends_anything(
    feishu: FakeFeishu, tmp_path: Path
) -> None:
    """The module needs the app mode. In webhook mode the deploy keeps its own rollback
    text (common.deploy.promote), and the card steps do nothing."""
    assert rc.open_release_card(WEBHOOK_ENV, _card("requested")) == ""
    for stage in rc.STAGES:
        assert rc.advance_release_card(WEBHOOK_ENV, "om_card", _card(stage)) is False
    assert rc.reply_release_failure(WEBHOOK_ENV, "om_card", "reason") is False
    assert (
        rc.note_rollback({**WEBHOOK_ENV, rc.MESSAGE_ID_ENV: "om_card"}, EVENT) is False
    )
    output = tmp_path / "github_output"
    assert (
        rc.main(
            ["open", "--stage", "requested", "--github-output", str(output)],
            env=WEBHOOK_ENV,
        )
        == 0
    )

    assert feishu.calls == []
    assert output.read_text(encoding="utf-8") == "message_id=\n"


@pytest.mark.parametrize("env", [APP_ENV, WEBHOOK_ENV])
def test_a_failing_feishu_never_raises(monkeypatch, env: dict) -> None:
    def broken(_request, timeout):
        raise URLError("network down")

    monkeypatch.setattr("common.alerting.urlopen", broken)

    assert rc.open_release_card(env, _card("awaiting_approval")) == ""
    assert rc.advance_release_card(env, "om_card", _card("failed")) is False
    assert rc.reply_release_failure(env, "om_card", "reason") is False
    rc.note_progress({**env, rc.MESSAGE_ID_ENV: "om_card"}, "sync", "done")
    assert rc.note_rollback({**env, rc.MESSAGE_ID_ENV: "om_card"}, EVENT) is False


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
    handled = rc.note_rollback({**APP_ENV, rc.MESSAGE_ID_ENV: "om_card"}, EVENT)

    assert handled is True
    methods = [(c["method"], c["url"].rsplit("/", 1)[-1]) for c in feishu.sends()]
    assert methods == [("PATCH", "om_card"), ("POST", "reply")]
    patched = json.loads(feishu.sends()[0]["body"]["content"])
    assert "Rolled back" in patched["header"]["title"]["content"]
    reply = json.loads(feishu.sends()[1]["body"]["content"])["text"]
    assert "Action: rolled_back" in reply
    assert "Reason: llm is unhealthy" in reply


@pytest.mark.parametrize(
    "action",
    [
        "paused",
        "blocked_by_schema_class_c",
        "circuit_breaker_tripped",
        "rollback_failed: dokploy refused the redeploy",
    ],
)
def test_an_event_that_is_not_a_completed_rollback_shows_failed_with_the_action_first(
    feishu: FakeFeishu, tmp_path: Path, action: str
) -> None:
    """Only the action rolled_back is a rollback. The others need a person. The action
    and the manual-intervention line come before the long reason, so the clip of the
    card and of the reply never cuts them."""
    github_env = tmp_path / "github_env"
    env = {**APP_ENV, rc.MESSAGE_ID_ENV: "om_card", "GITHUB_ENV": str(github_env)}
    event = {
        **EVENT,
        "action": action,
        "manual_intervention_required": True,
        "reason": "unhealthy: " + "x" * 1300,
    }

    assert rc.note_rollback(env, event) is True

    patched = json.loads(feishu.sends()[0]["body"]["content"])
    title = patched["header"]["title"]["content"]
    assert "Failed" in title and "Rolled back" not in title
    assert f"Action: {action}" in json.dumps(patched)
    reply = json.loads(feishu.sends()[1]["body"]["content"])["text"]
    assert f"Action: {action}" in reply
    assert "Manual intervention required: True" in reply
    assert len(reply) <= rc.MAX_REPLY_CHARS
    assert github_env.read_text(encoding="utf-8") == "RELEASE_CARD_STAGE=failed\n"


def test_a_rollback_of_a_run_without_a_card_is_left_to_the_text_path(
    feishu: FakeFeishu,
) -> None:
    assert rc.note_rollback(APP_ENV, EVENT) is False
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

    handled = rc.note_rollback({**APP_ENV, rc.MESSAGE_ID_ENV: "om_card"}, EVENT)

    assert handled is True
    assert [c["url"].rsplit("/", 1)[-1] for c in fake.sends()] == ["reply"]


def _gh_run_created(command, **_kwargs):
    assert command[:3] == [
        "gh",
        "api",
        "repos/wangzitian0/infra2/actions/runs/39947166819",
    ]
    return subprocess.CompletedProcess(command, 0, "2026-10-23T11:35:39Z\n", "")


def test_the_accept_until_time_is_the_one_that_the_deploy_job_enforces() -> None:
    """The card must show the time that production_request_accept_until returns, the
    function of the age check. A sender deadline moves it earlier than the default."""
    from infra2_sdk.deploy import SENDER_DEADLINE_KEY

    from common.deploy.app_deploy_request import production_request_accept_until

    created = datetime(2026, 10, 23, 11, 35, 39, tzinfo=timezone.utc)
    fmt = rc.ACCEPT_UNTIL_FORMAT

    without = rc.accept_until_text(APP_ENV, run=_gh_run_created)

    # The time carries the date: a sender deadline can put it hours away.
    assert without.startswith("2026-10-23 ")
    assert without.endswith(" UTC")

    assert without == production_request_accept_until(created, None).strftime(fmt)
    deadline = created + timedelta(seconds=600)
    env = {
        **APP_ENV,
        "APP_DEPLOY_REQUEST_JSON": json.dumps(
            {**REQUEST, SENDER_DEADLINE_KEY: deadline.isoformat()}
        ),
    }
    with_deadline = rc.accept_until_text(env, run=_gh_run_created)
    assert with_deadline == production_request_accept_until(created, deadline).strftime(
        fmt
    )
    assert with_deadline != without


def test_an_unreadable_run_time_gives_no_accept_until_and_no_error() -> None:
    def broken_run(command, **_kwargs):
        raise subprocess.CalledProcessError(1, command)

    assert rc.accept_until_text(APP_ENV, run=broken_run) == ""


def test_the_cli_shows_the_accept_until_time_only_when_asked(
    feishu: FakeFeishu, monkeypatch
) -> None:
    monkeypatch.setattr(rc, "accept_until_text", lambda _env: "11:59 UTC")
    env = {**APP_ENV, rc.MESSAGE_ID_ENV: "om_card"}

    assert (
        rc.main(["advance", "--stage", "awaiting_approval", "--accept-until"], env=env)
        == 0
    )
    assert rc.main(["advance", "--stage", "awaiting_approval"], env=env) == 0

    with_time, without_time = (c["body"]["content"] for c in feishu.sends())
    assert "before 11:59 UTC" in with_time
    assert "before" not in without_time


def test_a_rollback_is_remembered_for_the_later_steps_of_the_job(
    feishu: FakeFeishu, tmp_path: Path
) -> None:
    github_env = tmp_path / "github_env"
    env = {**APP_ENV, rc.MESSAGE_ID_ENV: "om_card", "GITHUB_ENV": str(github_env)}

    assert rc.note_rollback(env, EVENT) is True

    assert github_env.read_text(encoding="utf-8") == "RELEASE_CARD_STAGE=rolled_back\n"


@pytest.mark.parametrize("stage", sorted(rc.REASON_STAGES))
def test_the_failed_step_does_not_overwrite_a_card_that_has_a_reason(
    feishu: FakeFeishu, stage: str
) -> None:
    env = {**APP_ENV, rc.MESSAGE_ID_ENV: "om_card", rc.STAGE_ENV: stage}

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


def test_a_failing_card_hook_does_not_stop_the_rollback_text(
    feishu: FakeFeishu, monkeypatch
) -> None:
    from common.deploy import promote

    for key, value in {**APP_ENV, rc.MESSAGE_ID_ENV: "om_card"}.items():
        monkeypatch.setenv(key, value)

    def broken(_env, _event):
        raise RuntimeError("card code is broken")

    monkeypatch.setattr(rc, "note_rollback", broken)

    promote._notify_rollback_event(EVENT)

    assert feishu.sends()[0]["url"].endswith("receive_id_type=chat_id")


def test_a_failing_card_hook_does_not_stop_the_deploy_progress(monkeypatch) -> None:
    from common.deploy import v2

    def broken(_env, _what, _outcome):
        raise RuntimeError("card code is broken")

    monkeypatch.setattr(rc, "note_progress", broken)

    v2._progress("sync", "completed", time.monotonic())


@pytest.mark.parametrize("payload", ["{not json", "null", "[]", "42"])
def test_a_malformed_request_never_raises_and_never_calls_feishu(
    feishu: FakeFeishu, capsys, payload: str
) -> None:
    env = {**APP_ENV, "APP_DEPLOY_REQUEST_JSON": payload, rc.MESSAGE_ID_ENV: "om_card"}

    rc.note_progress(env, "sync", "completed")
    assert rc.note_rollback(env, EVENT) is False
    assert rc.main(["advance", "--stage", "deploying"], env=env) == 0
    assert rc.main(["open", "--stage", "requested"], env=env) == 0

    assert feishu.calls == []
    assert capsys.readouterr().err.count("::warning title=Release card::") >= 4


def test_a_hung_feishu_cannot_hold_the_deploy(monkeypatch) -> None:
    """A socket timeout applies to each read, and a DNS lookup has none. The bound is the
    thread join, so a call that never returns costs the deploy the budget and no more."""
    release = threading.Event()

    def hang(_request, timeout):
        release.wait(30)
        raise URLError("released")

    monkeypatch.setattr("common.alerting.urlopen", hang)
    monkeypatch.setattr(rc, "PROGRESS_BUDGET_SECONDS", 0.3)
    monkeypatch.setattr(rc, "ROLLBACK_BUDGET_SECONDS", 0.3)
    env = {**APP_ENV, rc.MESSAGE_ID_ENV: "om_card"}

    started = time.monotonic()
    rc.note_progress(env, "sync", "completed")
    handled = rc.note_rollback(env, EVENT)
    elapsed = time.monotonic() - started
    release.set()

    assert handled is False
    assert elapsed < 3


def test_the_time_budgets_stay_small() -> None:
    assert rc.PROGRESS_TIMEOUT_SECONDS <= 5
    assert rc.PROGRESS_BUDGET_SECONDS <= 10
    assert rc.ROLLBACK_BUDGET_SECONDS <= 30


def test_a_reply_hides_a_secret_and_stays_within_the_limit(feishu: FakeFeishu) -> None:
    rc.reply_release_failure(
        APP_ENV, "om_card", "failed: password=hunter2 " + "y" * 6000
    )

    text = json.loads(feishu.sends()[0]["body"]["content"])["text"]
    assert "hunter2" not in text
    assert len(text) <= rc.MAX_REPLY_CHARS


def test_a_message_id_with_a_newline_never_reaches_the_step_output(
    monkeypatch, tmp_path: Path
) -> None:
    def odd(request, timeout):
        if request.full_url.endswith("/tenant_access_token/internal"):
            return _Response({"code": 0, "tenant_access_token": "tenant-token"})
        return _Response({"code": 0, "data": {"message_id": "om_x\nmessage_id=evil"}})

    monkeypatch.setattr("common.alerting.urlopen", odd)
    output = tmp_path / "github_output"

    rc.main(
        ["open", "--stage", "requested", "--github-output", str(output)], env=APP_ENV
    )

    assert output.read_text(encoding="utf-8") == "message_id=\n"


def test_a_message_id_cannot_change_the_url_path(feishu: FakeFeishu) -> None:
    alerting.update_feishu_app_card(
        app_id="cli_test",
        app_secret="app-credential",
        message_id="om_a/../b?x=1",
        card={},
    )

    assert feishu.sends()[0]["url"].endswith("/messages/om_a%2F..%2Fb%3Fx%3D1")


def test_the_generic_names_are_the_fallback_and_values_are_trimmed() -> None:
    assert (
        alerting.out_of_band_app_config({"ALERT_DELIVERY_MODE": " feishu_app "})["mode"]
        == "feishu_app"
    )
    assert (
        alerting.out_of_band_app_config({"FEISHU_WEBHOOK_URL": "  https://hook  "})[
            "webhook_url"
        ]
        == "https://hook"
    )
    assert (
        alerting.out_of_band_app_config(
            {"INFRA2_OUT_OF_BAND_FEISHU_WEBHOOK_URL": "  https://hook  "}
        )["webhook_url"]
        == "https://hook"
    )


def test_the_detail_on_the_card_is_clipped() -> None:
    card = rc.render_release_card(_card("failed", detail="Q" * 5000))

    assert json.dumps(card).count("Q") == rc.MAX_DETAIL_CHARS


def test_the_hook_threads_are_daemons_so_a_stalled_call_cannot_keep_the_process(
    monkeypatch,
) -> None:
    release = threading.Event()
    seen: list[threading.Thread] = []

    def hang(_request, timeout):
        seen.append(threading.current_thread())
        release.wait(30)
        raise URLError("released")

    monkeypatch.setattr("common.alerting.urlopen", hang)
    monkeypatch.setattr(rc, "PROGRESS_BUDGET_SECONDS", 0.2)

    rc.note_progress({**APP_ENV, rc.MESSAGE_ID_ENV: "om_card"}, "sync", "completed")
    release.set()

    assert seen and all(thread.daemon for thread in seen)
    assert seen[0].name == "release-card"


def test_after_a_stalled_progress_update_later_progress_updates_are_skipped(
    monkeypatch,
) -> None:
    release = threading.Event()
    calls: list[str] = []

    def hang(request, timeout):
        calls.append(request.full_url)
        release.wait(30)
        raise URLError("released")

    monkeypatch.setattr("common.alerting.urlopen", hang)
    monkeypatch.setattr(rc, "PROGRESS_BUDGET_SECONDS", 0.2)
    env = {**APP_ENV, rc.MESSAGE_ID_ENV: "om_card"}

    rc.note_progress(env, "sync", "completed")
    started = time.monotonic()
    rc.note_progress(env, "wait for health", "completed")
    elapsed = time.monotonic() - started
    release.set()

    assert len(calls) == 1
    assert elapsed < 0.1


def test_a_stalled_progress_update_cannot_overwrite_the_rollback_that_follows(
    monkeypatch,
) -> None:
    """The progress update stalls, its budget ends, and the rollback goes out. The stalled
    update must reach Feishu before the rollback does, so the card ends as rolled back."""
    applied: list[str] = []

    def slow_progress(request, timeout):
        body = json.loads(request.data.decode("utf-8")) if request.data else {}
        content = body.get("content", "")
        if request.get_method() == "PATCH" and "Deploying" in content:
            time.sleep(0.8)
        if request.full_url.endswith("/tenant_access_token/internal"):
            return _Response({"code": 0, "tenant_access_token": "tenant-token"})
        if request.get_method() == "PATCH":
            title = json.loads(content)["header"]["title"]["content"]
            applied.append("Deploying" if "Deploying" in title else "Rolled back")
        return _Response({"code": 0})

    monkeypatch.setattr("common.alerting.urlopen", slow_progress)
    monkeypatch.setattr(rc, "PROGRESS_BUDGET_SECONDS", 0.2)
    env = {**APP_ENV, rc.MESSAGE_ID_ENV: "om_card"}

    rc.note_progress(env, "sync", "completed")
    assert rc.note_rollback(env, EVENT) is True

    assert applied == ["Deploying", "Rolled back"]


def test_the_text_fallback_of_a_rollback_is_bounded_too(monkeypatch) -> None:
    from common.deploy import promote

    release = threading.Event()

    def hang(_request, timeout):
        release.wait(30)
        raise URLError("released")

    monkeypatch.setattr("common.alerting.urlopen", hang)
    monkeypatch.setattr(rc, "ROLLBACK_BUDGET_SECONDS", 0.3)
    for key, value in APP_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv(rc.MESSAGE_ID_ENV, raising=False)

    started = time.monotonic()
    promote._notify_rollback_event(EVENT)
    elapsed = time.monotonic() - started
    release.set()

    assert elapsed < 3


def test_when_the_card_calls_are_rejected_the_rollback_text_still_goes_out(
    monkeypatch,
) -> None:
    """The app can hold the send permission and lack the update permission. Feishu then
    answers the update and the reply with an error code. The text must still go out."""
    from common.deploy import promote

    calls: list[tuple[str, str]] = []

    def no_update_permission(request, timeout):
        url = request.full_url
        calls.append((request.get_method(), url.rsplit("/", 1)[-1]))
        if url.endswith("/tenant_access_token/internal"):
            return _Response({"code": 0, "tenant_access_token": "tenant-token"})
        if request.get_method() == "PATCH" or url.endswith("/reply"):
            return _Response({"code": 99991672, "msg": "no permission"})
        return _Response({"code": 0, "data": {"message_id": "om_text"}})

    monkeypatch.setattr("common.alerting.urlopen", no_update_permission)
    for key, value in {**APP_ENV, rc.MESSAGE_ID_ENV: "om_card"}.items():
        monkeypatch.setenv(key, value)

    promote._notify_rollback_event(EVENT)

    sends = [c for c in calls if c[1] != "internal"]
    assert [method for method, _ in sends] == ["PATCH", "POST", "POST"]
    assert sends[-1][1].endswith("chat_id")


def test_a_bounded_dispatch_failure_in_rollback_fallback_warns_and_does_not_raise(
    monkeypatch,
) -> None:
    """When release_card.bounded raises during the fallback rollback alert dispatch,
    _notify_rollback_event logs a warning and does not propagate the exception."""
    from common.deploy import promote

    warnings: list[str] = []

    def boom(_fn, _budget, _default):
        raise RuntimeError("thread pool exhausted")

    monkeypatch.setattr(rc, "note_rollback", lambda _env, _event: False)
    monkeypatch.setattr(rc, "bounded", boom)
    monkeypatch.setattr(promote, "warning", warnings.append)

    promote._notify_rollback_event(EVENT)

    assert warnings == [
        "out-of-band rollback alert delivery skipped/failed: thread pool exhausted"
    ]
