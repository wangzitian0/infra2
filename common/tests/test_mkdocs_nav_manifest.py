"""mkdocs nav must list every MANIFEST SSOT topic — the nav is a hand-curated index, so it
drifts from MANIFEST silently (it had ~7 topics missing + stale '(Planned)' labels on live
docs before this guard). We enforce COMPLETENESS rather than generating the nav: the nav
carries curated labels + nesting that don't live in MANIFEST, and a mis-generated nested YAML
would break the docs build — so the right tool is "nav ⊇ MANIFEST topics", not generation.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
MKDOCS = ROOT / "docs/mkdocs.yml"
MANIFEST = ROOT / "docs/ssot/MANIFEST.yaml"

_NAV_SSOT_RE = re.compile(r"ssot/([\w.-]+\.(?:md|ya?ml))")


def test_mkdocs_nav_lists_every_manifest_ssot_topic() -> None:
    nav_files = set(_NAV_SSOT_RE.findall(MKDOCS.read_text(encoding="utf-8")))
    entries = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["entries"]
    owners = {entry["owner"].split("/")[-1] for entry in entries.values()}

    missing = sorted(owners - nav_files)
    assert not missing, (
        "MANIFEST SSOT topics missing from docs/mkdocs.yml nav (add them under `- SSOT:`):\n"
        + "\n".join(missing)
    )
