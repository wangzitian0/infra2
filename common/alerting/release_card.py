"""One Feishu card for each production release.

A production deploy request waits for the owner before it deploys. Before this module,
the only trace of the wait, of the deploy phases and of the result was a log line. This
module keeps one interactive card in the out-of-band chat. The receiver creates the card
before the approval gate and updates the same card as the release moves. A rollback or a
failure also gets a reply in the thread of the card.

Every function is best effort and never raises. A notification must not stop a deploy.

The card needs the Feishu app mode (``feishu_app``). A webhook cannot update a card. In
webhook mode, and when no card exists, the module sends one plain text message for the
stages that need a person: waiting for approval, rolled back and failed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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

#: The stages where a person must look. They get a text message when no card can carry them.
PERSON_STAGES = frozenset({"awaiting_approval", "rolled_back", "failed"})

MAX_DETAIL_CHARS = 600

#: A progress update runs inside the deploy. It waits this long at most.
PROGRESS_TIMEOUT_SECONDS = 3.0

#: The step variable that tells the later steps of a job which stage a rollback set.
STAGE_ENV = "RELEASE_CARD_STAGE"


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
            "An approval that comes after the request expires has no effect."
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
    config = common.alerting.out_of_band_app_config(env)
    try:
        if config["mode"] != "feishu_app":
            _text_for_a_person(env, card, timeout)
            return ""
        response = common.alerting.deliver_feishu_app_card(
            app_id=config["app_id"],
            app_secret=config["app_secret"],
            chat_id=config["chat_id"],
            api_base=config["api_base"],
            card=render_release_card(card),
            timeout=timeout,
        )
        return common.alerting.message_id_of(response)
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
    """Replace the card with the new state. Return true when the card or the text went out."""
    config = common.alerting.out_of_band_app_config(env)
    try:
        if not message_id or config["mode"] != "feishu_app":
            return _text_for_a_person(env, card, timeout)
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
    """Reply in the thread of the card with the reason. Return true when a reply went out."""
    config = common.alerting.out_of_band_app_config(env)
    safe_text = redact_secrets(text)[:MAX_DETAIL_CHARS]
    try:
        if message_id and config["mode"] == "feishu_app":
            common.alerting.reply_feishu_app_text(
                app_id=config["app_id"],
                app_secret=config["app_secret"],
                message_id=message_id,
                text=safe_text,
                api_base=config["api_base"],
                timeout=timeout,
            )
            return True
        common.alerting.deliver_out_of_band_text(env, safe_text, timeout=timeout)
        return True
    except Exception as exc:  # noqa: BLE001 - a notification never stops a deploy
        _warn("reply", exc)
        return False


def note_progress(env: Mapping[str, str], what: str, outcome: str) -> None:
    """Show one finished deploy phase on the card of this run, when the run has a card."""
    message_id = env.get(MESSAGE_ID_ENV, "")
    if not message_id:
        return
    try:
        card = card_from_environment(
            env, "deploying", f"{what}: {outcome or 'unknown'}"
        )
    except Exception as exc:  # noqa: BLE001 - a notification never stops a deploy
        _warn("progress", exc)
        return
    advance_release_card(env, message_id, card, timeout=PROGRESS_TIMEOUT_SECONDS)


def note_rollback(env: Mapping[str, str], reason_text: str) -> bool:
    """Mark the card rolled back and reply with the reason.

    Return false when the run has no card or when both Feishu calls failed. The caller
    then sends the text message."""
    message_id = env.get(MESSAGE_ID_ENV, "")
    if not message_id:
        return False
    try:
        first_line = (reason_text.splitlines() or [""])[0]
        card = card_from_environment(env, "rolled_back", first_line)
    except Exception as exc:  # noqa: BLE001 - a notification never stops a deploy
        _warn("rollback", exc)
        return False
    updated = advance_release_card(env, message_id, card)
    replied = reply_release_failure(env, message_id, reason_text)
    if updated or replied:
        _remember_stage(env, "rolled_back")
    return updated or replied


def expiry_text(
    env: Mapping[str, str],
    seconds: int,
    run: Callable[..., Any] = subprocess.run,
) -> str:
    """The time ``seconds`` after this run was created, as ``HH:MM UTC``.

    An empty string when the run time cannot be read. The card then shows no time."""
    try:
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
        expires = created.astimezone(timezone.utc) + timedelta(seconds=seconds)
        return expires.strftime("%H:%M UTC")
    except Exception as exc:  # noqa: BLE001 - a notification never stops a deploy
        _warn("expiry", exc)
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


def _text_for_a_person(
    env: Mapping[str, str], card: ReleaseCard, timeout: float
) -> bool:
    """One plain text message for a stage that needs a person. No other stage sends text.

    Return true when a message went out."""
    if card.stage not in PERSON_STAGES:
        return False
    label = STAGES[card.stage][1]
    lines = [
        f"[{label}] {card.service} {card.environment} {card.version_ref}",
        card.detail,
        card.run_url,
    ]
    common.alerting.deliver_out_of_band_text(
        env, "\n".join(line for line in lines if line), timeout=timeout
    )
    return True


def _warn(action: str, exc: Exception) -> None:
    print(
        f"::warning title=Release card::{action} failed: {type(exc).__name__}",
        file=sys.stderr,
        flush=True,
    )


def main(
    argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None
) -> int:
    """The step entry: ``open``, ``advance`` and ``reply``. The exit code is always 0."""
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("open", "advance"):
        command = sub.add_parser(name)
        command.add_argument("--stage", choices=sorted(STAGES), required=True)
        command.add_argument("--detail", default="")
        command.add_argument("--expires-at", default="")
        command.add_argument("--expires-after-run-created", type=int, default=0)
    sub.choices["open"].add_argument("--github-output", default="")
    sub.choices["advance"].add_argument("--message-id", default="")
    reply = sub.add_parser("reply")
    reply.add_argument("--text", required=True)
    reply.add_argument("--message-id", default="")
    args = parser.parse_args(argv)

    try:
        if args.command != "reply":
            if args.expires_after_run_created and not args.expires_at:
                args.expires_at = expiry_text(env, args.expires_after_run_created)
            if args.stage == "failed" and env.get(STAGE_ENV) == "rolled_back":
                # A rollback already marked the card and replied with its reason.
                return 0
        if args.command == "open":
            card = card_from_environment(env, args.stage, args.detail, args.expires_at)
            message_id = open_release_card(env, card)
            if args.github_output:
                with open(args.github_output, "a", encoding="utf-8") as handle:
                    handle.write(f"message_id={message_id}\n")
        elif args.command == "advance":
            message_id = args.message_id or env.get(MESSAGE_ID_ENV, "")
            card = card_from_environment(env, args.stage, args.detail, args.expires_at)
            advance_release_card(env, message_id, card)
        else:
            message_id = args.message_id or env.get(MESSAGE_ID_ENV, "")
            reply_release_failure(env, message_id, args.text)
    except Exception as exc:  # noqa: BLE001 - a notification never stops a deploy
        _warn(args.command, exc)
    return 0
