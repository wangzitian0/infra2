#!/usr/bin/env python3
"""Report what advancing a submodule pin would change in this repository's inputs.

AGENTS.md says a submodule pin "只表示开发快照，不是 package、runtime、deployment
或 config-hash 依赖". That is not quite true, and the gap is the reason this
exists: ``libs/app_manifests.ensure_present`` resolves app manifests from
``raw.githubusercontent.com/<owner>/<repo>/<pinned-sha>/<path>``, and
``libs/secrets_registry.load_manifest`` calls it at deploy time, because neither
infra-ci nor the iac-runner checks the submodules out. The pinned commit
therefore selects which ``required-env`` contract a deploy validates against.

The consequence is narrow but real: advance a pin across an App commit that adds
a required variable, and the next deploy demands a secret that may not exist in
1Password yet.

So the honest rule is neither "pins are free" nor "every pin advance needs
review". It is: **a pin advance is inert when it changes none of the files this
repository derives inputs from, and needs a decision when it changes one.** That
is a measurable property, which is what this reports.

The derived set is discovered rather than listed, so a new consumer cannot
silently escape the check: any ``repos/<sub>/<path>`` literal appearing in
tracked non-test source is treated as derived.

Exit 0 when every pin advance is inert (or there is nothing to advance), 1 when
an advance would change a derived input. Exit 1 is not "refuse" — it means this
one needs a human, with the diff to look at already printed.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SEARCH_DIRS = ("libs", "tools", "bootstrap", "platform", ".github")

# Prose lives in docstrings and comments too, and it quotes paths in backticks.
# Stripped per line, never across the file: a line holding an odd number of
# backticks -- this module's own pattern literal is one -- would otherwise
# misalign every pair after it and expose the prose it was meant to remove.
BACKTICKED_RE = re.compile(r"`+[^`\n]*`+")


def _git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def derived_inputs(sub_paths: list[str]) -> dict[str, set[str]]:
    """Files inside each submodule that this repository reads, as {sub: {path}}.

    Discovered from tracked source rather than declared, so adding a consumer
    extends the check automatically. One pattern per submodule, so a root-level
    submodule is covered exactly like a nested one.

    Two kinds of file are not evidence of derivation and are skipped:

    - **Tests.** A fixture naming a manifest path is not this repository
      deriving an input from it.
    - **Markdown.** Documentation *describing* a path -- including this tool's
      own entry in ``tools/README.md``, which lists all four manifests -- would
      otherwise make the check discover its own prose and report it as a
      consumer.

    Backtick-quoted spans are stripped for the same reason, since prose lives
    in Python docstrings too: a sibling checker's docstring cites another
    submodule's README as an example of a legitimate cross-reference, and
    without this the check called that README a derived input and would have
    demanded a decision every time the other repository touched it.

    Paths are matched only with a file extension, so a bare directory mention
    does not register.
    """
    pats = {
        p: re.compile(re.escape(p) + r"/([A-Za-z0-9_./-]+\.[A-Za-z0-9]+)")
        for p in sub_paths
    }
    found: dict[str, set[str]] = {p: set() for p in sub_paths}
    for rel in _git("ls-files", "--", *SEARCH_DIRS).splitlines():
        name = Path(rel).name
        if name.startswith("test_") or "/tests/" in rel or rel.endswith(".md"):
            continue
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        text = "\n".join(BACKTICKED_RE.sub(" ", ln) for ln in text.splitlines())
        for sub, pat in pats.items():
            found[sub].update(pat.findall(text))
    return found


def submodules() -> list[tuple[str, str]]:
    """``(path, pinned_sha)`` for every gitlink in HEAD, at any depth.

    Enumerated from the tree rather than from a ``repos/`` prefix. Scoping this
    to ``repos/`` was a real defect in the first version: ``.gitmodules``
    registers four submodules and ``oh-my-code-agent`` sits at the repository
    root, so a third of the pins were outside the check that exists to watch
    pins.
    """
    out = []
    for line in _git("ls-tree", "-r", "-t", "HEAD").splitlines():
        fields = line.split(maxsplit=3)
        if len(fields) == 4 and fields[1] == "commit":
            out.append((fields[3].strip(), fields[2]))
    return out


def main() -> int:
    subs = submodules()
    derived = derived_inputs([path for path, _ in subs])
    blocked = False
    checked = 0

    for name, pinned in subs:
        checkout = ROOT / name
        if not (checkout / ".git").exists():
            continue  # not checked out; nothing to advance from
        try:
            head = _git("rev-parse", "HEAD", cwd=checkout).strip()
        except subprocess.CalledProcessError:
            continue
        if head == pinned:
            continue

        checked += 1
        paths = sorted(derived.get(name, ()))
        if not paths:
            print(
                f"{name}: {pinned[:7]} -> {head[:7]} — inert "
                "(this repository derives no input from it)"
            )
            continue

        try:
            changed = _git(
                "diff", "--name-only", pinned, head, "--", *paths, cwd=checkout
            ).split()
        except subprocess.CalledProcessError:
            print(
                f"{name}: {pinned[:7]} -> {head[:7]} — CANNOT VERIFY: the pinned "
                "commit is not present in this checkout. Fetch it before advancing."
            )
            blocked = True
            continue

        if not changed:
            print(
                f"{name}: {pinned[:7]} -> {head[:7]} — inert "
                f"({len(paths)} derived input(s) unchanged)"
            )
            continue

        blocked = True
        print(f"{name}: {pinned[:7]} -> {head[:7]} — CHANGES DERIVED INPUTS:")
        for path in changed:
            print(f"    {path}")
        print(
            "    These select what a deploy validates against, so advancing this\n"
            "    pin is a deploy-affecting change, not a snapshot bump. Review the\n"
            "    diff and confirm every newly required secret exists before merging."
        )

    if checked == 0:
        print("submodule_pin_impact: no pin differs from its checkout.")
        return 0
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
