#!/usr/bin/env python3
"""Record the ops-checks watchdog's verdicts as GitHub issues (truealpha#876 W4).

The last step of the ops-checks ``watchdog`` job. It reads the verdict file the
earlier steps appended to (``INFRA2_WATCHDOG_VERDICTS_PATH``) and, per
``libs/watchdog_issue_trail.py``, opens or comments on one issue per red check
and closes the issues of checks that are green again. Feishu delivery is the
watchdog steps' own and is untouched.

Run kind decides what may be written (``issue_trail_mode``):

- ``schedule``, or a plain dispatch on main: open, comment and close;
- a dispatch with ``INFRA2_PEER_LIVENESS_BOUND_CAP_HOURS`` set (the drill), or a
  dispatch from another branch: open and comment, never close;
- a dry run or a dispatch with SSH target overrides: nothing.

    GITHUB_EVENT_NAME=schedule GITHUB_REPOSITORY=owner/name GITHUB_TOKEN=... \\
      INFRA2_WATCHDOG_VERDICTS_PATH=watchdog-verdicts.jsonl \\
      python tools/watchdog_issue_trail.py

Exit codes: 0 done (or nothing to do, or the open issues could not be listed);
1 a write failed, or the verdict path, repository or token is missing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.scheduler_peer_liveness import BOUND_CAP_ENV  # noqa: E402
from libs.watchdog_issue_trail import (  # noqa: E402
    OFF,
    VERDICTS_ENV,
    GitHubIssues,
    issue_trail_mode,
    load_trail,
    reconcile,
)


def run_context(env: Mapping[str, str]) -> str:
    """How this run was started, in words, for the issue body."""
    event = env.get("GITHUB_EVENT_NAME", "") or "unknown event"
    if event == "schedule":
        text = "scheduled run"
    else:
        text = f"{event} run on {env.get('GITHUB_REF', '') or 'an unknown ref'}"
    cap = (env.get(BOUND_CAP_ENV) or "").strip()
    if cap:
        text += f"; drill: peer-liveness bound capped at {cap} h, so it never closes an issue"
    return text


def _run_url(env: Mapping[str, str]) -> str:
    parts = [
        env.get(key, "")
        for key in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")
    ]
    if all(parts):
        return f"{parts[0]}/{parts[1]}/actions/runs/{parts[2]}"
    return ""


def main(env: Mapping[str, str] | None = None, *, issues_factory=GitHubIssues) -> int:
    current = os.environ if env is None else env
    mode = issue_trail_mode(current)
    if mode == OFF:
        print(
            "issue trail: off for this run (dry run, manual SSH diagnostics, or not scheduled/dispatched)"
        )
        return 0
    path = (current.get(VERDICTS_ENV) or "").strip()
    repository = (current.get("GITHUB_REPOSITORY") or "").strip()
    token = (current.get("GITHUB_TOKEN") or current.get("GH_TOKEN") or "").strip()
    if not path:
        print(f"::error::issue trail: {VERDICTS_ENV} is not set", file=sys.stderr)
        return 1
    if "/" not in repository or not token:
        print(
            "::error::issue trail: GITHUB_REPOSITORY (owner/name) and a token "
            "(GITHUB_TOKEN or GH_TOKEN) are required",
            file=sys.stderr,
        )
        return 1
    trail = load_trail(path)
    print(
        f"issue trail ({mode}): {len(trail.failing)} red "
        f"[{', '.join(sorted(trail.failing)) or 'none'}], {len(trail.green)} green, "
        f"green families {sorted(trail.green_prefixes) or 'none'}"
    )
    return reconcile(
        issues_factory(repository, token),
        trail,
        mode=mode,
        run_url=_run_url(current),
        context=run_context(current),
        env=current,
    )


if __name__ == "__main__":
    raise SystemExit(main())
