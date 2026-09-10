#!/usr/bin/env python3
"""Fail when a repository webhook is attempting deliveries and none of them land.

`/webhook` returned 401 on every delivery from at least 2026-07-20 to 2026-09-09 (#585)
— seven weeks in which no push to `main` reached the runner, and nothing anywhere said
so. It was found by accident, while investigating something else. The one place it was
recorded the whole time is the hook's own delivery list, which nothing read.

The signal is deliberately narrow: **attempted but never landing**. A quiet day with no
deliveries is not a finding, and neither is one bad delivery among good ones — a webhook
that has succeeded recently is working, whatever else it did.

    python3 tools/webhook_delivery_audit.py                     # every configured hook
    python3 tools/webhook_delivery_audit.py --repo owner/name

Reads only. GITHUB_TOKEN (or GH_TOKEN) must carry `admin:repo_hook` read scope; without a
token the audit reports that it could not look, rather than passing quietly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

DEFAULT_REPO = "wangzitian0/infra2"
#: How many recent deliveries to consider. One page is ~30, which at this repository's
#: cadence spans several days — long enough that a working hook is certain to appear in
#: it, short enough that a fault from months ago cannot mask a recovery.
DELIVERY_WINDOW = 30
_API = "https://api.github.com"


@dataclass(frozen=True)
class HookVerdict:
    hook_id: int
    url: str
    updated_at: str
    attempted: int
    succeeded: int
    last_status: str

    @property
    def failing(self) -> bool:
        return self.attempted > 0 and self.succeeded == 0

    def line(self) -> str:
        if not self.attempted:
            return f"  hook {self.hook_id} {self.url}: no deliveries in the window"
        verdict = (
            "NONE LANDED" if self.failing else f"{self.succeeded}/{self.attempted} ok"
        )
        return (
            f"  hook {self.hook_id} {self.url}: {verdict}"
            f" (last {self.last_status}; secret last set {self.updated_at})"
        )


def _get(path: str, token: str) -> list | dict:
    request = urllib.request.Request(
        f"{_API}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def verdicts(repo: str, token: str, *, get=None) -> list[HookVerdict]:
    # Resolved at call time, not bound as a default: a default argument would capture
    # the original function and make the reader at the module level un-substitutable.
    get = get or _get
    out: list[HookVerdict] = []
    for hook in get(f"/repos/{repo}/hooks", token):
        hook_id = int(hook["id"])
        deliveries = get(f"/repos/{repo}/hooks/{hook_id}/deliveries", token)
        window = list(deliveries)[:DELIVERY_WINDOW]
        succeeded = sum(1 for d in window if 200 <= int(d.get("status_code", 0)) < 300)
        out.append(
            HookVerdict(
                hook_id=hook_id,
                url=str(hook.get("config", {}).get("url", "")),
                # A secret rotated on one side only is the shape this failure takes, and
                # the hook's own updated_at is when GitHub's copy was last written (#585,
                # and again the same morning for CF_API_TOKEN).
                updated_at=str(hook.get("updated_at", "")),
                attempted=len(window),
                succeeded=succeeded,
                last_status=str(window[0].get("status_code", "?")) if window else "-",
            )
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not token:
        print("webhook deliveries: not checked (no GITHUB_TOKEN)", file=sys.stderr)
        return 1
    try:
        found = verdicts(args.repo, token)
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError) as exc:
        print(f"webhook deliveries: could not read {args.repo}: {exc}", file=sys.stderr)
        return 1

    failing = [v for v in found if v.failing]
    print(
        f"webhook deliveries: {len(found)} hook(s), {len(failing)} attempting and landing nothing"
    )
    for verdict in found:
        print(verdict.line())
    return 1 if failing else 0


if __name__ == "__main__":
    raise SystemExit(main())
