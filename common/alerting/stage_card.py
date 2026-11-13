"""One Feishu card for each stage task, rendered from its stage records (#1645).

A stage task (a staging soak of one release tag, a production promotion) writes the records
``pre``, ``operation`` and ``post``. This module renders the records of one (env, subject) as
one card: the failed check ids first, then the passed ones, with the seconds of each check, the
env, the tag and the run URL. The card has at most ``MAX_CHECK_LINES`` check lines.

The reconcile run creates the card after the ``pre`` record and updates it by message id after
each later record. Only the Feishu app can update a card. In webhook mode there is no card: the
records are still written and ``show_stage_card`` prints ``::warning::``. It never raises, and it
never waits longer than ``STAGE_CARD_BUDGET_SECONDS`` for Feishu: a card must not stop a deploy.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from typing import Any

import common.alerting
from common.alerting.card import build_feishu_report_card
from common.alerting.release_card import bounded, publish_card
from common.alerting.types import redact_secrets
from infra2_sdk.stage_record import StageRecord

MAX_CHECK_LINES = 12
MAX_DETAIL_CHARS = 80
STAGE_CARD_BUDGET_SECONDS = 15.0
PHASES = ("pre", "operation", "post")


def _label(records: Sequence[StageRecord]) -> tuple[str, str]:
    """(Feishu header colour, state) of the records so far."""
    if any(r.verdict == "fail" for r in records):
        return "red", "Failed"
    if any(r.phase == "post" for r in records):
        return "green", "Passed"
    return "blue", f"Running: {records[-1].phase} done"


def render_stage_card(records: Sequence[StageRecord]) -> dict[str, Any]:
    """The card of the records of one stage. The records share one env and one subject."""
    if not records or len({(r.env, r.subject) for r in records}) != 1:
        raise ValueError("a stage card needs the records of exactly one stage")
    ordered = sorted(records, key=lambda r: PHASES.index(r.phase))
    last = ordered[-1]
    template, state = _label(ordered)
    checks = [(r.phase, c) for r in ordered for c in r.checks]
    checks.sort(key=lambda item: item[1].ok)  # failed first; the sort keeps phase order
    lines = [
        f"{'✅' if c.ok else '❌'} {phase}/{c.name} · {c.seconds:g} s"
        + (
            f" · {redact_secrets(c.detail)[:MAX_DETAIL_CHARS]}"
            if not c.ok and c.detail
            else ""
        )
        for phase, c in checks[:MAX_CHECK_LINES]
    ]
    if len(checks) > MAX_CHECK_LINES:
        lines[-1] = f"… {len(checks) - MAX_CHECK_LINES + 1} more checks"
    rendered = build_feishu_report_card(
        title=f"{last.env} {last.subject}: {state}",
        template=template,
        kpi_fields=[
            ("Environment", last.env),
            ("Tag", last.subject),
            ("Phases", ", ".join(r.phase for r in ordered)),
        ],
        sections=["\n".join(lines)],
        actions=[("Open the run", last.run_url, "default")],
    )
    # Feishu updates a card only when it is shared before and after the update.
    rendered["config"]["update_multi"] = True
    return rendered


def show_stage_card(
    env: Mapping[str, str], records: Sequence[StageRecord], message_id: str = ""
) -> str:
    """Create the card (no ``message_id``) or update it. Return the message id, or ``message_id``."""
    try:
        if common.alerting.out_of_band_app_config(env)["mode"] != "feishu_app":
            print(
                "::warning title=Stage card::Feishu is in webhook mode, which cannot "
                "update a card; the stage records are written and no card is sent",
                file=sys.stderr,
                flush=True,
            )
            return message_id
        card = render_stage_card(records)
        return (
            bounded(
                lambda: publish_card(env, card, message_id),
                STAGE_CARD_BUDGET_SECONDS,
                message_id,
            )
            or message_id
        )
    except Exception as exc:  # noqa: BLE001 - a card never stops a deploy
        print(
            f"::warning title=Stage card::{type(exc).__name__}: no card update",
            file=sys.stderr,
            flush=True,
        )
        return message_id
