#!/usr/bin/env python3
"""Daily reminder: a release tag is green in staging and not in production (#1645).

The tag counts as green in staging when its ``staging.release_validation`` post record passes.
It counts as in production when a passing ``prod.release_integrity`` post record or a
``production/<tag>`` marker exists, or when a newer marker exists. The newest tag that is green
and not in production gets one card: the tag, its staging post record and the promote command.
The command is text. This tool dispatches nothing and writes nothing: the owner promotes.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.alerting import (  # noqa: E402
    build_feishu_report_card,
    deliver_out_of_band_card,
)
from common.deploy.release_markers import (  # noqa: E402
    PRODUCTION_MARKER_PREFIX,
    version_key,
)
from common.deploy.github_api import get_json  # noqa: E402
from common.deploy.stage_record_reader import AGENT, list_records, open_url  # noqa: E402

LOOKBACK_DAYS = 30
PROMOTE_COMMAND = (
    "gh workflow run reconcile-iac-inputs.yml --ref main "
    "-f promote_prod=true -f after={tag}"
)


def pending_promotion(
    staging: Iterable[Mapping[str, Any]],
    production: Iterable[Mapping[str, Any]],
    marker_tags: Iterable[str],
) -> Mapping[str, Any] | None:
    """The staging post record of the newest tag that is green in staging and not in production."""
    markers = {t.removeprefix(PRODUCTION_MARKER_PREFIX) for t in marker_tags}
    promoted = {
        r["subject"].removeprefix("tag:") for r in production if r["verdict"] == "pass"
    }
    newest_marker = max(map(version_key, markers), default=())
    green = {
        r["subject"].removeprefix("tag:"): r
        for r in staging
        if r["phase"] == "post" and r["verdict"] == "pass"
    }
    open_tags = [
        tag for tag in green if tag not in promoted and version_key(tag) > newest_marker
    ]
    return green[max(open_tags, key=version_key)] if open_tags else None


def reminder_card(record: Mapping[str, Any]) -> dict[str, Any]:
    tag = record["subject"].removeprefix("tag:")
    return build_feishu_report_card(
        title=f"{tag} is green in staging and not in production",
        template="orange",
        kpi_fields=[
            ("Tag", tag),
            ("Staging post record", f"{record['verdict']} at {record['finished_at']}"),
            ("Units", ", ".join(record.get("units", ())) or "none"),
        ],
        sections=[
            "Promote it (the owner approves the run):\n"
            f"`{PROMOTE_COMMAND.format(tag=tag)}`"
        ],
        actions=[("Staging run", record["run_url"], "default")],
    )


def main() -> int:
    env = os.environ
    repo, token = env.get("GITHUB_REPOSITORY", ""), env.get("GITHUB_TOKEN", "")
    if not (repo and token):
        print(
            "promotion_reminder: GITHUB_REPOSITORY and GITHUB_TOKEN are required",
            file=sys.stderr,
        )
        return 2
    since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    staging = list_records(
        repo, token, env="staging.release_validation", phase="post", since=since
    )
    production = list_records(
        repo, token, env="prod.release_integrity", phase="post", since=since
    )
    refs = get_json(
        f"/repos/{repo}/git/matching-refs/tags/{PRODUCTION_MARKER_PREFIX}",
        token,
        open_url,
        AGENT,
    )
    record = pending_promotion(
        staging, production, [r["ref"].removeprefix("refs/tags/") for r in refs]
    )
    if record is None:
        print("promotion_reminder: no release tag waits for production")
        return 0
    card = reminder_card(record)
    if os.environ.get("PROMOTION_REMINDER_DRY_RUN") == "1":
        print(card)
        return 0
    try:
        deliver_out_of_band_card(
            env, card, fallback_text=card["header"]["title"]["content"]
        )
    except Exception as exc:  # noqa: BLE001 - a reminder never fails the run
        print(f"::warning::promotion reminder not delivered: {type(exc).__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
