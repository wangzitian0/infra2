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

Submodules are skipped, by the paths ``.gitmodules`` registers rather than by
directory name: they are separate repositories with their own CI, and
AGENTS.md is explicit that harness policy is not distributed into them. Name
matching would also exclude this repository's own top-level ``truealpha/``
and ``finance_report/``, which merely share a basename with two of them.
"""

from __future__ import annotations

import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def submodule_paths() -> set[str]:
    """Paths registered in .gitmodules, as repo-relative POSIX strings.

    A link INTO a submodule is legitimate -- the root README pointing at
    ``oh-my-code-agent/README.md`` is exactly the kind of cross-reference
    AGENTS.md's entry map is built from -- but it cannot be verified here.
    CI checks out without ``--recursive``, so those directories are empty,
    and a checker that treated an empty submodule as a dead link would fail
    on every correct link into one. Registration is the right signal: the
    path is declared by this repository, and its contents are the other
    repository's CI to verify.
    """
    paths: set[str] = set()
    gitmodules = os.path.join(REPO_ROOT, ".gitmodules")
    if not os.path.exists(gitmodules):
        return paths
    for line in open(gitmodules, encoding="utf-8"):
        line = line.strip()
        if line.startswith("path"):
            _, _, value = line.partition("=")
            if value.strip():
                paths.add(value.strip().rstrip("/"))
    return paths


SUBMODULES = None  # populated in main()

# Skipped by NAME at any depth: build and tooling noise that is never
# documentation.
SKIP_NAMES = {
    ".git", ".venv", "node_modules", "__pycache__",
    # Agent scratch checkouts; gitignored, not repository content.
    ".claude", ".omo",
}

# Skipped by repo-relative PATH. Name matching was wrong here and Copilot
# caught it: this repository has a top-level `truealpha/` and
# `finance_report/` that are ordinary tracked directories with their own
# Markdown, distinct from the `repos/truealpha` and `repos/finance_report`
# submodules that merely share a basename. Skipping by name silently
# excluded 12 real documents from checking.
#
# The submodule paths themselves come from .gitmodules rather than being
# listed here, so this set stays empty unless something genuinely needs a
# path-scoped exclusion.
SKIP_PATHS: set[str] = set()

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


def iter_markdown_files(submodules: set[str]):
    skip_paths = SKIP_PATHS | submodules
    for root, dirs, files in os.walk(REPO_ROOT):
        rel_root = os.path.relpath(root, REPO_ROOT).replace(os.sep, "/")
        prefix = "" if rel_root == "." else rel_root + "/"
        dirs[:] = [
            d for d in dirs
            if d not in SKIP_NAMES and (prefix + d) not in skip_paths
        ]
        for name in files:
            if name.endswith(".md"):
                yield os.path.join(root, name)


def main() -> int:
    global SUBMODULES
    SUBMODULES = submodule_paths()
    dead: list[tuple[str, str, str]] = []
    exempt: list[tuple[str, str]] = []

    for path in iter_markdown_files(SUBMODULES):
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
            rel_target = os.path.relpath(resolved, REPO_ROOT).replace(os.sep, "/")
            if any(rel_target == sm or rel_target.startswith(sm + "/") for sm in SUBMODULES):
                continue  # inside a submodule: unverifiable without a recursive checkout
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
