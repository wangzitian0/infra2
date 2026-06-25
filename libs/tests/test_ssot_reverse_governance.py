"""Close the governance loop: every file in docs/ssot/ must be GOVERNED.

test_ssot_governance enforces MANIFEST -> files (every manifest topic has a real owner). This
is the other direction, files -> MANIFEST: a doc must not sit in docs/ssot/ with no owner, no
proof, governed by nobody. Without this, a new SSOT doc added without a MANIFEST entry is
silently ungoverned (it caught ops.standards.md + deploy-dependencies.yaml, now governed).

Only explicit, non-topic files are allowed to be absent from MANIFEST: the human index, the
template, the manifest itself, and the redirect stubs left by topic merges.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SSOT_DIR = ROOT / "docs/ssot"
MANIFEST = SSOT_DIR / "MANIFEST.yaml"

# Files that are legitimately NOT MANIFEST topics. Keep this list SHORT and justified —
# anything here is consciously exempted from ownership, not silently ungoverned.
_ALLOWED_NON_TOPICS = {
    "README.md",  # the generated human index (tools/gen_ssot_index.py)
    "template.md",  # the authoring template
    "MANIFEST.yaml",  # the registry itself
    "ops.alerting.md",  # redirect stub -> ops.obs (topic merged)
    "ops.availability-ledger.md",  # redirect stub -> ops.obs (topic merged)
}


def test_every_ssot_doc_is_governed() -> None:
    entries = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["entries"]
    owned = {entry["owner"].split("/")[-1] for entry in entries.values()}

    ungoverned = sorted(
        path.name
        for path in SSOT_DIR.iterdir()
        if path.is_file()
        and path.suffix in {".md", ".yaml", ".yml"}
        and path.name not in owned
        and path.name not in _ALLOWED_NON_TOPICS
    )
    assert not ungoverned, (
        "docs/ssot files with no MANIFEST owner (give them an entry, or — if truly not a "
        "topic — add to _ALLOWED_NON_TOPICS with a reason):\n" + "\n".join(ungoverned)
    )
