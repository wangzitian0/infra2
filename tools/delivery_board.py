#!/usr/bin/env python3
"""Daily delivery board: one Lark card and one SigNoz log record (#1644 Q1).
# alerts-as: monthly-ops-summary

The logic is in ``common.observability.delivery_board``. This tool reads the stage records,
the merged PRs and the tree. It writes the record to SigNoz and sends the card to the reports
chat. It dispatches nothing. A failed write or a failed send prints a warning: the board is a
report, and the card of the next day shows the same numbers.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.alerting import deliver_report_card  # noqa: E402
from common.observability.delivery_board import (  # noqa: E402
    board_card,
    board_lines,
    collect_board,
    write_signoz,
)


def main() -> int:
    env = os.environ
    repo, token = env.get("GITHUB_REPOSITORY", ""), env.get("GITHUB_TOKEN", "")
    if not (repo and token):
        print(
            "delivery_board: GITHUB_REPOSITORY and GITHUB_TOKEN are required",
            file=sys.stderr,
        )
        return 2
    board = collect_board(repo, token, root=ROOT)
    text = "\n".join(board_lines(board))
    print(text)
    if env.get("DELIVERY_BOARD_DRY_RUN") == "1":
        return 0
    signoz_error = write_signoz(board, env)
    if signoz_error:
        print(f"::warning::delivery board record not written to SigNoz: {signoz_error}")
    try:
        deliver_report_card(
            env,
            board_card(board, repo=repo, signoz_error=signoz_error),
            fallback_text=text,
        )
    except Exception as exc:  # noqa: BLE001 - a report never fails the run
        print(f"::warning::delivery board card not delivered: {type(exc).__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
