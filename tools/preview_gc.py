#!/usr/bin/env python3
"""Garbage-collect orphaned Dokploy preview stacks (infra2-owned, reconcile-based).

Preview stacks leak because teardown is imperative: a per-alias ``preview_lifecycle
down`` can be skipped (the bare ``main`` slot left a ``finance-report-preview-main``
compose running for weeks after the rename to ``branch-main``) or fail mid-flight
(infra2#310). Rather than trust every teardown to fire, this is the declarative
backstop: list the preview composes Dokploy actually runs (under
``finance_report``/``preview``), compute what *should* exist, and reap the
difference. It is infra2's own concern (Dokploy is an infra mechanism) and needs
no app-repo dispatch — it pulls the desired set itself.

Reaped, conservatively, are only two unambiguous orphan classes:
  1. **Pre-rename bare-slug aliases** — a preview alias with no current kind prefix
     (``branch-``/``pr-``/``tag-``), e.g. the bare ``main`` slot the model replaced
     with ``branch-main``. The deterministic-name ``down`` can no longer reach it.
  2. **``pr-<n>`` previews for CLOSED PRs** — a leaked PR teardown.

Everything else (``branch-main``, the reserved canary ``pr-<_CANARY_PR>`` slot,
``tag-*``, any non-preview compose) is left untouched. Fail-safe: if the open-PR
set can't be fetched, PR reaping is skipped (bare-slug orphans still go, since they
don't depend on PR state). Dry-run by default; pass ``--apply`` to actually delete.

Run on the hourly ops-checks cron so convergence is bounded regardless of when a
leak happens.
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
from tools.deploy_env_config import (  # noqa: E402
    PREVIEW_ENVIRONMENT,
    PREVIEW_PROJECT,
    _PREVIEW_SLUG_PREFIX,
)
from tools.deploy_v2 import _CANARY_PR  # noqa: E402

# The bare main-tip preview that the current model always wants up.
ALWAYS_KEEP_ALIASES = frozenset({"branch-main"})
# The reserved canary slot (deploy_v2_canary stands it up/tears it down hourly);
# never reap it even when PR 999 is obviously not "open".
CANARY_ALIAS = f"pr-{_CANARY_PR}"
# Every alias the current model emits is `<kind>-<slug>`; an alias with no known
# kind prefix (e.g. the bare `main` slug from before the branch-main rename) is a
# pre-rename orphan the deterministic-name `down` can no longer reach.
VALID_KIND_PREFIXES = ("branch-", "pr-", "tag-")
APP_REPO = "wangzitian0/finance_report"
_COMPOSE_PREFIX = f"{_PREVIEW_SLUG_PREFIX}-"


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
                "User-Agent": "infra2-preview-gc/1.0",
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


def reap(
    client, orphans: list[tuple[PreviewCompose, str]], *, apply: bool
) -> list[str]:
    """Delete (or, in dry-run, describe) each orphan; return human log lines."""
    lines: list[str] = []
    for c, why in orphans:
        if apply:
            client.delete_compose(c.compose_id, delete_volumes=True)
            lines.append(f"REAPED {c.compose_name} — {why}")
        else:
            lines.append(f"DRY-RUN would reap {c.compose_name} — {why}")
    return lines


def run(
    client, *, token: str | None, apply: bool, opener=urllib.request.urlopen
) -> dict:
    projects = client.list_projects()
    composes = collect_preview_composes(projects)
    open_prs = fetch_open_pr_numbers(token, opener=opener)
    orphans = select_orphans(composes, open_pr_numbers=open_prs)
    log = reap(client, orphans, apply=apply)
    return {
        "preview_composes_seen": len(composes),
        "open_pr_fetch": "ok"
        if open_prs is not None
        else "unavailable (PR reap skipped)",
        "reaped" if apply else "would_reap": len(orphans),
        "log": log,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually delete orphans (default: dry-run, only report)",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("PREVIEW_GC_GH_TOKEN") or os.getenv("GH_PAT"),
        help="GitHub token to read the app repo's open PRs (default: env "
        "PREVIEW_GC_GH_TOKEN / GH_PAT). Absent -> PR reap skipped, fail-safe.",
    )
    args = parser.parse_args(argv)

    from libs.dokploy import get_dokploy

    result = run(get_dokploy(), token=args.token, apply=args.apply)
    for line in result["log"]:
        print(line)
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "log"},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
