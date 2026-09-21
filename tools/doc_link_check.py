#!/usr/bin/env python3
"""Fail when a relative Markdown link in this repository points at nothing.

AGENTS.md builds the whole wiki on cross-references — the 0/1 級 entry map,
the 互引原則 that entries must point at each other, every SSOT citation. None
of that was checked by anything, and it had drifted: 29 links were dead when
this check was first written, essentially all of them positional rather than
substantive.

Two mechanisms produced them, and both recur:

1. Archiving a project moves it into ``docs/project/archive/`` and every
   referrer keeps the old path.
2. The moved file's own relative links are now one directory deeper, so each
   needs one more ``../``.

Neither is a judgement call, which is exactly why leaving them to review was
the wrong place for them.

Submodules are skipped: ``repos/*`` and ``oh-my-code-agent`` are separate
repositories with their own CI, and AGENTS.md is explicit that harness policy
is not distributed into them.
"""

from __future__ import annotations

import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_DIRS = {
    ".git", ".venv", "node_modules", "__pycache__",
    # Submodules: separate repositories, separate CI (AGENTS.md "依賴邊界").
    "repos", "oh-my-code-agent", "truealpha",
    # Agent scratch checkouts; gitignored, not repository content.
    ".claude", ".omo",
}

LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+?)\)")

# Keyed on (file, link target with any anchor stripped), so an entry stays
# matched when only the section anchor changes.
#
# Links that are known-dead and deliberately not guessed at, each with the
# decision that is actually pending. An entry here is a tracked question, not
# a silenced failure: if the answer were mechanical it would be a fix instead.
KNOWN_UNRESOLVED = {
    (
        "e2e_regressions/tests/data/postgresql/README.md",
        "../../../../docs/ssot/db.business_pg.md",
    ): (
        "This suite calls itself the Test Anchor for a Business PostgreSQL SSOT "
        "that does not exist. docs/ssot/ has db.platform_pg.md instead, and "
        "retargeting is not obviously correct: platform_pg describes 'Platform "
        "層的共享 PostgreSQL', its Proof section is numbered 4 while this link "
        "cites #5, and it carries no back-reference to this suite. So either "
        "business PG lost its SSOT and needs one, or the two are the same "
        "subject under two names. That is a content decision, not a path fix."
    ),
}


def iter_markdown_files():
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name.endswith(".md"):
                yield os.path.join(root, name)


def main() -> int:
    dead: list[tuple[str, str, str]] = []
    exempt: list[tuple[str, str]] = []

    for path in iter_markdown_files():
        rel_file = os.path.relpath(path, REPO_ROOT)
        try:
            text = open(path, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        for match in LINK_RE.finditer(text):
            label, link = match.group(1), match.group(2)
            if link.startswith(("http://", "https://", "mailto:", "#", "//")):
                continue
            target = link.split("#", 1)[0]
            if not target:
                continue  # pure anchor within the same file
            resolved = os.path.normpath(os.path.join(os.path.dirname(path), target))
            if os.path.exists(resolved):
                continue
            if (rel_file, target) in KNOWN_UNRESOLVED:
                exempt.append((rel_file, target))
                continue
            dead.append((rel_file, link, label))

    if exempt:
        print(f"{len(exempt)} known-unresolved link(s), each tracking a pending decision:")
        for rel_file, link in exempt:
            print(f"  {rel_file} -> {link}")
            print(f"      {KNOWN_UNRESOLVED[(rel_file, link)]}")
        print()

    if not dead:
        print("doc_link_check: every relative Markdown link resolves.")
        return 0

    print(f"doc_link_check: {len(dead)} dead relative link(s):\n")
    for rel_file, link, label in dead:
        print(f"  {rel_file}")
        print(f"      [{label}]({link})")
    print(
        "\nMost dead links here are positional. The two that recur:\n"
        "  - a project was archived into docs/project/archive/ and a referrer "
        "still points at the old path;\n"
        "  - a file moved one directory deeper and its own relative links each "
        "need one more '../'.\n"
        "If a link is genuinely unresolvable because the target's existence is "
        "an open question, add it to KNOWN_UNRESOLVED with the decision that is "
        "pending — never a guessed target."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
