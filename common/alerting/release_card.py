"""One Feishu card for each production release.

A production deploy request waits for the owner before it deploys. Before this module,
the only trace of the wait, of the deploy phases and of the result was a log line. This
module keeps one interactive card in the out-of-band chat. The receiver creates the card
before the approval gate and updates the same card as the release moves. A rollback or a
failure also gets a reply in the thread of the card.

Every function is best effort and never raises. A notification must not stop a deploy.
Every call that runs inside the deploy process has a time bound that holds even when a
network call hangs (``_bounded``).

Known gap: a rejected or expired approval, and a run cancelled before the deploy job
starts, run no step after the approval gate, so the card stays at "Waiting for approval".
The card shows the accept-until time, so a viewer sees that it is stale. A job that closes
the card would hold the Feishu secrets in a third job, and that is a production job
classification edit in ``common.gate.production_contract`` (owner approval of the head SHA).

The card needs the Feishu app mode (``feishu_app``). A webhook cannot update a card, so in
webhook mode there is no card: ``open_release_card`` returns an empty id, and every later
step does nothing. The rollback text of ``common.deploy.promote`` stays the notice then.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import common.alerting
from common.alerting.card import build_feishu_report_card
from common.alerting.types import redact_secrets

#: The environment variable that carries the id of the card across steps and processes.
MESSAGE_ID_ENV = "RELEASE_CARD_MESSAGE_ID"

#: stage -> (Feishu header colour, label). The stage is the state of the release.
STAGES: dict[str, tuple[str, str]] = {
    "requested": ("grey", "Request accepted"),
    "awaiting_approval": ("orange", "Waiting for approval"),
    "deploying": ("blue", "Deploying"),
    "succeeded": ("green", "Succeeded"),
    "rolled_back": ("red", "Rolled back"),
    "failed": ("red", "Failed"),
}

MAX_DETAIL_CHARS = 600

#: The thread reply carries the whole rollback event. The reason is its last line.
MAX_REPLY_CHARS = 3000

#: A progress update runs inside the deploy. Each request waits this long at most.
PROGRESS_TIMEOUT_SECONDS = 3.0

#: The whole progress update (a token request and an update request) ends within this time.
PROGRESS_BUDGET_SECONDS = 6.0

#: The whole rollback notice (an update and a thread reply) ends within this time.
ROLLBACK_BUDGET_SECONDS = 20.0

#: The step variable that tells the later steps of a job that a rollback event wrote the
#: reason on the card. The generic "failed" step then does not replace that reason.
STAGE_ENV = "RELEASE_CARD_STAGE"

#: The stages that carry a reason. A later generic "failed" update must not replace them.
REASON_STAGES = frozenset({"rolled_back", "failed"})

#: The shape of a Feishu message id. The id goes into a URL path and into a step output.
MESSAGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


@dataclass(frozen=True)
class ReleaseCard:
    """What the card shows. Each update replaces the whole card from this value."""

    stage: str
    service: str
    environment: str
    version_ref: str
    source_sha: str
    run_url: str
    detail: str = ""
    approval_expires_at: str = ""

    def __post_init__(self) -> None:
        if self.stage not in STAGES:
            raise ValueError(f"unknown release stage: {self.stage}")


def render_release_card(card: ReleaseCard) -> dict[str, Any]:
    """Build the interactive card. The card is shared, so every viewer sees each update."""
    template, label = STAGES[card.stage]
    sections = []
    if card.detail:
        sections.append(redact_secrets(card.detail)[:MAX_DETAIL_CHARS])
    if card.stage == "awaiting_approval":
        expiry = (
            f" before {card.approval_expires_at}" if card.approval_expires_at else ""
        )
        sections.append(
            f"The owner approves the deploy in GitHub{expiry}. "
            "An approval that comes after the request expires has no effect. "
            "When that time has passed, this card is stale: the request expired "
            "or the owner rejected it."
        )
    actions = [
        (
            "Open the run",
            card.run_url,
            "primary" if card.stage == "awaiting_approval" else "default",
        )
    ]
    rendered = build_feishu_report_card(
        title=f"{card.service} {card.environment} {card.version_ref}: {label}",
        template=template,
        kpi_fields=[
            ("Service", card.service),
            ("Environment", card.environment),
            ("Version", card.version_ref),
            ("Commit", card.source_sha[:8]),
        ],
        sections=sections,
        actions=actions,
    )
    # Feishu updates a card only when it is shared before and after the update.
    rendered["config"]["update_multi"] = True
    return rendered


def card_from_environment(
    env: Mapping[str, str], stage: str, detail: str = "", expires_at: str = ""
) -> ReleaseCard:
    """The card of the request that this receiver run handles."""
    request = json.loads(env.get("APP_DEPLOY_REQUEST_JSON") or "{}")
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    run_url = (
        f"{server}/{env['GITHUB_REPOSITORY']}/actions/runs/{env['GITHUB_RUN_ID']}"
        if env.get("GITHUB_REPOSITORY") and env.get("GITHUB_RUN_ID")
        else ""
    )
    return ReleaseCard(
        stage=stage,
        service=str(request.get("service", "unknown")),
        environment=str(request.get("deploy_type", "unknown")),
        version_ref=str(request.get("version_ref", "unknown")),
        source_sha=str(request.get("source_sha", "")),
        run_url=run_url,
        detail=detail,
        approval_expires_at=expires_at,
    )


def open_release_card(
    env: Mapping[str, str], card: ReleaseCard, *, timeout: float = 10.0
) -> str:
    """Create the card. Return its message id, or an empty string when none exists."""
    try:
        config = common.alerting.out_of_band_app_config(env)
        if config["mode"] != "feishu_app":
            return ""
        response = common.alerting.deliver_feishu_app_card(
            app_id=config["app_id"],
            app_secret=config["app_secret"],
            chat_id=config["chat_id"],
            api_base=config["api_base"],
            card=render_release_card(card),
            timeout=timeout,
        )
        message_id = common.alerting.message_id_of(response)
        if message_id and not MESSAGE_ID_PATTERN.match(message_id):
            raise ValueError("the Feishu message id has an unexpected shape")
        return message_id
    except Exception as exc:  # noqa: BLE001 - a notification never stops a deploy
        _warn("open", exc)
        return ""


def advance_release_card(
    env: Mapping[str, str],
    message_id: str,
    card: ReleaseCard,
    *,
    timeout: float = 10.0,
) -> bool:
    """Replace the card with the new state. Return true when the update went out."""
    try:
        config = common.alerting.out_of_band_app_config(env)
        if not message_id or config["mode"] != "feishu_app":
            return False
        common.alerting.update_feishu_app_card(
            app_id=config["app_id"],
            app_secret=config["app_secret"],
            message_id=message_id,
            card=render_release_card(card),
            api_base=config["api_base"],
            timeout=timeout,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - a notification never stops a deploy
        _warn("advance", exc)
        return False


def reply_release_failure(
    env: Mapping[str, str], message_id: str, text: str, *, timeout: float = 10.0
) -> bool:
    """Reply in the thread of the card. Return true when a reply went out."""
    try:
        config = common.alerting.out_of_band_app_config(env)
        if not message_id or config["mode"] != "feishu_app":
            return False
        common.alerting.reply_feishu_app_text(
            app_id=config["app_id"],
            app_secret=config["app_secret"],
            message_id=message_id,
            text=redact_secrets(text)[:MAX_REPLY_CHARS],
            api_base=config["api_base"],
            timeout=timeout,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - a notification never stops a deploy
        _warn("reply", exc)
        return False


def note_progress(env: Mapping[str, str], what: str, outcome: str) -> None:
    """Show one finished deploy phase on the card of this run, when the run has a card."""
    message_id = env.get(MESSAGE_ID_ENV, "")
    if not message_id:
        return

    def update() -> bool:
        card = card_from_environment(
            env, "deploying", f"{what}: {outcome or 'unknown'}"
        )
        return advance_release_card(
            env, message_id, card, timeout=PROGRESS_TIMEOUT_SECONDS
        )

    _bounded(update, PROGRESS_BUDGET_SECONDS, False)


def note_rollback(env: Mapping[str, str], event: Mapping[str, Any]) -> bool:
    """Show a rollback event on the card and in its thread.

    ``event`` is the event dictionary of ``common.deploy.promote``. Only the action
    ``rolled_back`` is a completed rollback. Every other action (a paused rollback, a
    rollback that failed, a blocked schema class, a tripped breaker) needs a person, so
    the card shows ``failed`` with the action first. Return false when the run has no card
    or when both Feishu calls failed. The caller then sends the text message."""
    message_id = env.get(MESSAGE_ID_ENV, "")
    if not message_id:
        return False
    return _bounded(
        lambda: _show_rollback(env, message_id, event), ROLLBACK_BUDGET_SECONDS, False
    )


def _show_rollback(
    env: Mapping[str, str], message_id: str, event: Mapping[str, Any]
) -> bool:
    stage = "rolled_back" if event.get("action") == "rolled_back" else "failed"
    lines = [
        f"Action: {event.get('action')}",
        f"Manual intervention required: {bool(event.get('manual_intervention_required'))}",
        f"Version: {event.get('from_version')} -> {event.get('to_version') or 'none'}",
        f"Reason: {event.get('reason')}",
    ]
    try:
        card = card_from_environment(env, stage, "\n".join(lines))
    except Exception as exc:  # noqa: BLE001 - a notification never stops a deploy
        _warn("rollback", exc)
        return False
    headline = (
        f"Deploy rollback event for {event.get('service')} ({event.get('environment')})"
    )
    updated = advance_release_card(env, message_id, card)
    replied = reply_release_failure(env, message_id, "\n".join([headline, *lines]))
    if updated or replied:
        _remember_stage(env, stage)
    return updated or replied


def accept_until_text(
    env: Mapping[str, str], run: Callable[..., Any] = subprocess.run
) -> str:
    """The last time at which the receiver accepts this request, as ``HH:MM UTC``.

    The time comes from ``production_request_accept_until``, the function that the age
    check of the deploy job uses. It reads the creation time of this run from the GitHub
    API and the sender deadline from the request. An empty string when it cannot be
    computed. The card then shows no time."""
    try:
        from common.deploy.app_deploy_request import (
            parse_sender_deadline,
            production_request_accept_until,
        )

        reply = run(
            [
                "gh",
                "api",
                f"repos/{env['GITHUB_REPOSITORY']}/actions/runs/{env['GITHUB_RUN_ID']}",
                "--jq",
                ".created_at",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
            env=dict(env),
        )
        created = datetime.fromisoformat(reply.stdout.strip().replace("Z", "+00:00"))
        deadline = parse_sender_deadline(
            json.loads(env.get("APP_DEPLOY_REQUEST_JSON") or "{}")
        )
        accept_until = production_request_accept_until(created, deadline)
        return accept_until.astimezone(timezone.utc).strftime("%H:%M UTC")
    except Exception as exc:  # noqa: BLE001 - a notification never stops a deploy
        _warn("accept-until", exc)
        return ""


def _remember_stage(env: Mapping[str, str], stage: str) -> None:
    """Tell the later steps of the job which stage the card has now."""
    path = env.get("GITHUB_ENV")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{STAGE_ENV}={stage}\n")
    except OSError as exc:
        _warn("stage", exc)


def _bounded(call: Callable[[], Any], seconds: float, default: Any) -> Any:
    """Run ``call`` in a daemon thread. Return its result, or ``default`` after ``seconds``.

    A socket timeout applies to each read, so a server that sends one byte at a time
    can hold a request for minutes, and a DNS lookup has no timeout. The thread is a
    daemon: it cannot keep the deploy process alive."""
    result: list[Any] = [default]

    def target() -> None:
        try:
            result[0] = call()
        except Exception as exc:  # noqa: BLE001 - a notification never stops a deploy
            _warn("bounded call", exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        print(
            f"::warning title=Release card::no answer within {seconds:g} s",
            file=sys.stderr,
            flush=True,
        )
        return default
    return result[0]


def _warn(action: str, exc: Exception) -> None:
    print(
        f"::warning title=Release card::{action} failed: {type(exc).__name__}",
        file=sys.stderr,
        flush=True,
    )


def main(
    argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None
) -> int:
    """The step entry: ``open`` and ``advance``. The workflow bounds the call with
    ``timeout`` and turns a failed call into a warning (``|| echo``). Bad arguments
    still make a non-zero exit here."""
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("open", "advance"):
        command = sub.add_parser(name)
        command.add_argument("--stage", choices=sorted(STAGES), required=True)
        command.add_argument("--detail", default="")
        command.add_argument(
            "--accept-until",
            action="store_true",
            help="show the last time at which the receiver accepts this request",
        )
    sub.choices["open"].add_argument("--github-output", default="")
    sub.choices["advance"].add_argument("--message-id", default="")
    args = parser.parse_args(argv)

    try:
        if args.stage == "failed" and env.get(STAGE_ENV) in REASON_STAGES:
            # A rollback event already wrote its reason on the card and in the thread.
            return 0
        expires_at = accept_until_text(env) if args.accept_until else ""
        card = card_from_environment(env, args.stage, args.detail, expires_at)
        if args.command == "open":
            message_id = open_release_card(env, card)
            if args.github_output:
                with open(args.github_output, "a", encoding="utf-8") as handle:
                    handle.write(f"message_id={message_id}\n")
        else:
            message_id = args.message_id or env.get(MESSAGE_ID_ENV, "")
            advance_release_card(env, message_id, card)
    except Exception as exc:  # noqa: BLE001 - a notification never stops a deploy
        _warn(args.command, exc)
    return 0
