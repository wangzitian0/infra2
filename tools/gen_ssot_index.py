#!/usr/bin/env python3
"""Generate the SSOT topic index in docs/ssot/README.md from MANIFEST.yaml.

The index tables are a pure projection of MANIFEST (file + key + summary, grouped by key
prefix), so they must NOT be hand-edited — change a topic's index line via MANIFEST `summary`.
`test_ssot_index_generated_matches_committed` locks generated == committed; run
`python tools/gen_ssot_index.py --write` to regenerate after editing MANIFEST.

This is the "central index construction" half of the doc-consistency work: one source
(MANIFEST), many generated views (this README index; mkdocs nav is the next view to fold in).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs/ssot/MANIFEST.yaml"
README = ROOT / "docs/ssot/README.md"

BEGIN = "<!-- BEGIN GENERATED SSOT INDEX (tools/gen_ssot_index.py) -->"
END = "<!-- END GENERATED SSOT INDEX -->"

# key prefix -> section header. Section order follows first appearance in MANIFEST order.
_CATEGORY = {
    "core": "Core - 核心 (必读)",
    "bootstrap": "Bootstrap - 引导层",
    "platform": "Platform - 平台层",
    "db": "Data - 数据层",
    "vault": "Data - 数据层",
    "ops": "Ops - 运维",
    "watchdog": "Ops - 运维",
}


def render_index() -> str:
    """The generated index block (between the markers), derived from MANIFEST."""
    entries = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["entries"]
    sections: list[tuple[str, list[str]]] = []
    for key, entry in entries.items():
        prefix = key.split(".", 1)[0]
        if prefix not in _CATEGORY:
            raise SystemExit(f"gen_ssot_index: no category for key prefix {prefix!r} ({key})")
        header = _CATEGORY[prefix]
        if not sections or sections[-1][0] != header:
            sections.append((header, []))
        file = entry["owner"].split("/")[-1]
        sections[-1][1].append(f"| [{file}](./{file}) | `{key}` | {entry['summary']} |")

    blocks = [
        f"## {header}\n\n| 文件 | SSOT Key | 关键内容 |\n|------|----------|----------|\n"
        + "\n".join(rows)
        for header, rows in sections
    ]
    return "\n\n---\n\n".join(blocks) + "\n\n---"


def render_readme() -> str:
    """Full README with the region between the markers replaced by the generated index."""
    text = README.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        raise SystemExit(f"gen_ssot_index: markers not found in {README} — add them once")
    pre = text.split(BEGIN)[0]
    post = text.split(END, 1)[1]
    return f"{pre}{BEGIN}\n\n{render_index()}\n\n{END}{post}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite README in place")
    args = parser.parse_args()

    expected = render_readme()
    if args.write:
        README.write_text(expected, encoding="utf-8")
        print(f"wrote {README}")
        return 0
    if README.read_text(encoding="utf-8") != expected:
        print("SSOT README index is stale — run: python tools/gen_ssot_index.py --write")
        return 1
    print("SSOT README index is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
