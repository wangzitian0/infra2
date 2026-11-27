#!/usr/bin/env python3
"""Daily delivery board: one Lark card and one SigNoz log record (#1644 Q1).
# alerts-as: monthly-ops-summary

The logic is in ``common.observability.delivery_board``. This tool reads the train run list,
the production markers, the merged PRs and the tree. It writes the record to SigNoz and sends
the card to the reports chat. It dispatches nothing. The board is a report: a failure never
fails the run, so the steps after it still run. A board that cannot be computed sends a card
that names the reason. A failed write shows on the card. A failed send prints a warning.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.alerting import deliver_report_card  # noqa: E402
from common.observability.delivery_board import (  # noqa: E402
    board_card,
    board_lines,
    collect_board,
    failure_card,
    write_signoz,
)


def _send(env, card: dict, text: str) -> None:
    try:
        deliver_report_card(env, card, fallback_text=text)
    except Exception as exc:  # noqa: BLE001 - a report never fails the run
        print(f"::warning::delivery board card not delivered: {type(exc).__name__}")


def main() -> int:
    env = os.environ
    repo, token = env.get("GITHUB_REPOSITORY", ""), env.get("GITHUB_TOKEN", "")
    if not (repo and token):
        print(
            "delivery_board: GITHUB_REPOSITORY and GITHUB_TOKEN are required",
            file=sys.stderr,
        )
        return 2
    dry_run = env.get("DELIVERY_BOARD_DRY_RUN") == "1"
    try:
        board = collect_board(repo, token, root=ROOT)
    except Exception as exc:  # noqa: BLE001 - the card names the reason
        reason = f"{type(exc).__name__}: {exc}"[:200]
        print(f"::warning::delivery board not computed: {reason}")
        if not dry_run:
            _send(env, failure_card(reason, now=datetime.now(timezone.utc)), reason)
        return 0
    text = "\n".join(board_lines(board))
    print(text)
    if dry_run:
        return 0
    signoz_error = write_signoz(board, env)
    if signoz_error:
        print(f"::warning::delivery board record not written to SigNoz: {signoz_error}")
    _send(env, board_card(board, repo=repo, signoz_error=signoz_error), text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
