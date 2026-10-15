"""Every workflow path filter must still point at something (#1021).

#1002 turned ``libs/alerting.py`` into the package ``libs/alerting/``. The
``apply-observability.yml`` push filter kept the old file name, so a change to the
SigNoz rule rendering in ``libs/alerting/signoz.py`` merged without the production
apply. The merge gate reads deploy triggers from the same filters, so it did not ask
the owner either. A filter that matches no file fails silently: the workflow simply
does not run.

Checked for ``push``, ``pull_request`` and ``pull_request_target``, both ``paths`` and
``paths-ignore``: a literal path must exist in the checkout, and a glob must match at
least one tracked file.
"""

from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
EVENTS = ("push", "pull_request", "pull_request_target")
KEYS = ("paths", "paths-ignore")


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return out.splitlines()


def _filters() -> list[tuple[str, str, str, str]]:
    rows = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        # PyYAML reads the bare key `on` as the boolean True.
        triggers = workflow.get(True) or workflow.get("on") or {}
        if not isinstance(triggers, dict):
            continue
        for event in EVENTS:
            spec = triggers.get(event)
            if not isinstance(spec, dict):
                continue
            for key in KEYS:
                for pattern in spec.get(key) or []:
                    rows.append((path.name, event, key, str(pattern)))
    return rows


FILTERS = _filters()
TRACKED = _tracked_files()


def test_the_filter_list_is_not_empty() -> None:
    """A parse that found nothing would make every case below pass vacuously."""
    assert len(FILTERS) >= 40, FILTERS


@pytest.mark.parametrize(
    ("workflow", "event", "key", "pattern"),
    FILTERS,
    ids=[f"{w}:{e}:{k}:{p}" for w, e, k, p in FILTERS],
)
def test_every_path_filter_matches_a_tracked_file(workflow, event, key, pattern):
    target = pattern.removeprefix("!")
    if any(ch in target for ch in "*?["):
        # GitHub's `**` matches across directories; fnmatch's `*` already does.
        matched = any(fnmatch.fnmatchcase(f, target) for f in TRACKED)
    else:
        matched = target in TRACKED or (ROOT / target).is_dir()
    assert matched, (
        f"{workflow} on.{event}.{key} lists {pattern!r}, which matches no tracked file. "
        "A filter that matches nothing never triggers the workflow: point it at the "
        "file's new location or remove it (#1021)."
    )
