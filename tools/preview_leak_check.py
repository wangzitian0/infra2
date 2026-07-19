#!/usr/bin/env python3
"""Detect (and, on demand, remediate) leaked Dokploy preview stacks — infra2-owned.

The lifecycle contract is **strict 1:1**: infra2 stands a preview up and tears it
down when its PR closes. A leftover preview is therefore an *exception* (a teardown
that was skipped or failed — e.g. the bare ``main`` slot stranded after the
``branch-main`` rename, or a mid-flight failure, infra2#310), NOT a routine state
to be silently swept by a periodic GC. So this tool **detects and ALERTS**; it does
not delete on a schedule. Remediation is a deliberate, SOP-driven manual step
(``--remediate``) — see docs/ssot/ops.pipeline.md#preview.

A preview is flagged as leaked when it is one of two unambiguous orphan classes:
  1. **Pre-rename bare-slug aliases** — a preview alias with no current kind prefix
     (``branch-``/``pr-``/``commit-``/``tag-``), e.g. the bare ``main`` slot the model
     replaced with ``branch-main``. The deterministic-name ``down`` can't reach it.
  2. **``pr-<n>`` previews for CLOSED PRs** — a leaked PR teardown.

Everything else (``branch-main``, the reserved canary ``pr-<_CANARY_PR>`` slot,
``tag-*``, any non-preview compose) is left untouched. Fail-safe: if the open-PR
set can't be fetched, PR leaks are not flagged (bare-slug orphans still are, since
they don't depend on PR state).

Modes:
- default (cron): **detect only**. Exit non-zero if any leak is found, so the
  ops-checks job fails and fires the out-of-band (Feishu) alert. Never deletes.
- ``--remediate``: delete the flagged leaks. Run by an operator following the SOP
  after an alert, once the leak is confirmed and root-caused.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.common import normalize_env_name  # noqa: E402
from libs.deploy_env_config import (  # noqa: E402
    PREVIEW_ENVIRONMENT,
    PREVIEW_KINDS,
    preview_alias,
    preview_service_config,
)
from tools.deploy_v2 import _CANARY_PR  # noqa: E402

# This tool is finance_report/app-scoped only (its open-PR fetch is hardcoded to
# APP_REPO below) — #522 generalized libs.deploy_env_config for multiple preview-capable
# services, but extending leak detection to scan every registered service's own
# Dokploy project is a separate, deliberate follow-up, not done here.
_PREVIEW_CONFIG = preview_service_config("finance_report/app")
PREVIEW_PROJECT = _PREVIEW_CONFIG.project

# The bare main-tip preview that the current model always wants up.
ALWAYS_KEEP_ALIASES = frozenset({"branch-main"})
# The reserved canary slot (deploy_v2_canary stands it up/tears it down hourly);
# never reap it even when PR 999 is obviously not "open".
CANARY_ALIAS = f"pr-{_CANARY_PR}"
# Every current alias is `<kind>-<slug>` for a known kind (branch/pr/commit/tag);
# an alias with no known kind prefix (e.g. the bare `main` slug from before the
# branch-main rename) is a pre-rename orphan the deterministic-name `down` can no
# longer reach. Derived from the public PREVIEW_KINDS so a new kind can't silently
# be misclassified as an orphan.
VALID_KIND_PREFIXES = tuple(f"{kind}-" for kind in PREVIEW_KINDS)
APP_REPO = "wangzitian0/finance_report"
# The compose-name prefix ("finance-report-preview-"), derived from the canonical
# builder (every compose_name is "<prefix>-<alias>") rather than a private constant.
_COMPOSE_PREFIX = preview_alias("pr", 1).compose_name.removesuffix("pr-1")


@dataclass(frozen=True)
class PreviewCompose:
    """A preview compose Dokploy is running under finance_report/preview."""

    compose_name: str
    compose_id: str
    alias: str


def collect_preview_composes(projects: list[dict]) -> list[PreviewCompose]:
    """Flatten the finance_report/preview environment into preview composes."""
    found: list[PreviewCompose] = []
    preview_env = normalize_env_name(PREVIEW_ENVIRONMENT)
    for project in projects:
        if str(project.get("name") or "") != PREVIEW_PROJECT:
            continue
        for env in project.get("environments") or []:
            if normalize_env_name(env.get("name")) != preview_env:
                continue
            for compose in env.get("compose") or []:
                cname = str(compose.get("name") or "")
                cid = compose.get("composeId")
                if not cid or not cname.startswith(_COMPOSE_PREFIX):
                    continue
                found.append(
                    PreviewCompose(
                        compose_name=cname,
                        compose_id=str(cid),
                        alias=cname[len(_COMPOSE_PREFIX) :],
                    )
                )
    return found


def orphan_reason(alias: str, *, open_pr_numbers: set[int] | None) -> str | None:
    """Why ``alias`` should be reaped, or None to keep it.

    Conservative by construction: keep branch-main, the canary slot, all tag-*,
    and (when the open-PR set is known) open PRs. Reap only (a) aliases with no
    valid kind prefix — pre-rename bare slugs like ``main`` — and (b) ``pr-<n>``
    for a PR that is provably closed. ``open_pr_numbers is None`` => PR state
    unknown => leave PR previews alone (fail-safe).
    """
    if alias in ALWAYS_KEEP_ALIASES or alias == CANARY_ALIAS:
        return None
    if not alias.startswith(VALID_KIND_PREFIXES):
        return f"pre-rename orphan alias '{alias}' (no current kind prefix)"
    if alias.startswith("pr-"):
        if open_pr_numbers is None:
            return None
        try:
            number = int(alias[len("pr-") :])
        except ValueError:
            return None
        if number not in open_pr_numbers:
            return f"closed-PR preview ({alias})"
    return None  # branch-* / tag-* / open pr -> keep


def select_orphans(
    composes: list[PreviewCompose],
    *,
    open_pr_numbers: set[int] | None,
) -> list[tuple[PreviewCompose, str]]:
    """Return (compose, reason) for each orphan to reap; everything else kept."""
    out: list[tuple[PreviewCompose, str]] = []
    for c in composes:
        reason = orphan_reason(c.alias, open_pr_numbers=open_pr_numbers)
        if reason is not None:
            out.append((c, reason))
    return out


def fetch_open_pr_numbers(
    token: str | None, *, opener=urllib.request.urlopen
) -> set[int] | None:
    """Open PR numbers for the app repo, or None if it can't be determined."""
    if not token:
        return None
    numbers: set[int] = set()
    page = 1
    while page <= 10:  # bound: 1000 PRs is far more than this repo ever has open
        url = (
            f"https://api.github.com/repos/{APP_REPO}/pulls"
            f"?state=open&per_page=100&page={page}"
        )
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "infra2-preview-leak-check/1.0",
            },
        )
        try:
            with opener(request, timeout=20) as response:
                batch = json.loads(response.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - any failure -> "unknown", fail safe
            return None
        if not isinstance(batch, list) or not batch:
            break
        for pr in batch:
            if isinstance(pr, dict) and isinstance(pr.get("number"), int):
                numbers.add(pr["number"])
        if len(batch) < 100:
            break
        page += 1
    return numbers


def remediate(client, leaks: list[tuple[PreviewCompose, str]]) -> list[str]:
    """Delete each confirmed leak (the SOP-driven manual step); return log lines."""
    lines: list[str] = []
    for c, why in leaks:
        client.delete_compose(c.compose_id, delete_volumes=True)
        lines.append(f"REMEDIATED {c.compose_name} — {why}")
    return lines


def detect(client, *, token: str | None, opener=urllib.request.urlopen) -> dict:
    """Detect leaked previews (no mutation). Pure: lists, classifies, reports."""
    projects = client.list_projects()
    composes = collect_preview_composes(projects)
    open_prs = fetch_open_pr_numbers(token, opener=opener)
    leaks = select_orphans(composes, open_pr_numbers=open_prs)
    return {
        "preview_composes_seen": len(composes),
        "open_pr_fetch": "ok"
        if open_prs is not None
        else "unavailable (PR leaks not flagged)",
        "leaks": leaks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--remediate",
        action="store_true",
        help="DELETE the detected leaks (SOP-driven manual step). Default: detect "
        "only — report and exit non-zero on a leak so the cron alerts.",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("PREVIEW_LEAK_GH_TOKEN") or os.getenv("GH_PAT"),
        help="GitHub token to read the app repo's open PRs (default: env "
        "PREVIEW_LEAK_GH_TOKEN / GH_PAT). Absent -> PR leaks not flagged, fail-safe.",
    )
    args = parser.parse_args(argv)

    from libs.dokploy import get_dokploy

    result = detect(get_dokploy(), token=args.token)
    leaks = result["leaks"]
    if leaks and args.remediate:
        result["log"] = remediate(get_dokploy(), leaks)
    else:
        result["log"] = [f"LEAK {c.compose_name} — {why}" for c, why in leaks]
    for line in result["log"]:
        print(line)
    print(
        json.dumps(
            {
                "preview_composes_seen": result["preview_composes_seen"],
                "open_pr_fetch": result["open_pr_fetch"],
                "leaks": len(leaks),
                "remediated": bool(leaks and args.remediate),
            },
            sort_keys=True,
        )
    )
    # Detect mode: a leak is an exception -> exit non-zero so the ops-checks job
    # fails and fires the out-of-band alert. Remediate mode: we just deleted the
    # confirmed leaks, so report success.
    if leaks and not args.remediate:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
