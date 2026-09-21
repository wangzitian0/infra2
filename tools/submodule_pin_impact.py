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

# A ``repos/<sub>/<path>`` file literal. Restricted to paths with an extension
# so bare directory mentions in prose do not register as derived inputs.
DERIVED_RE = re.compile(r"repos/([a-z0-9_-]+)/([A-Za-z0-9_./-]+\.[a-z]+)")

SEARCH_DIRS = ("libs", "tools", "bootstrap", "platform", ".github")


def _git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def derived_inputs() -> dict[str, set[str]]:
    """Files inside each submodule that this repository reads, as {sub: {path}}.

    Discovered from tracked source rather than declared, so adding a consumer
    extends the check automatically. Tests are excluded: a fixture naming a
    manifest path is not this repository deriving an input from it.
    """
    found: dict[str, set[str]] = {}
    for rel in _git("ls-files", "--", *SEARCH_DIRS).splitlines():
        name = Path(rel).name
        if name.startswith("test_") or "/tests/" in rel:
            continue
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for sub, path in DERIVED_RE.findall(text):
            found.setdefault(sub, set()).add(path)
    return found


def submodules() -> list[tuple[str, str]]:
    """``(name, pinned_sha)`` for each ``repos/*`` gitlink in HEAD."""
    out = []
    for line in _git("ls-tree", "HEAD", "repos/").splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[1] == "commit":
            out.append((Path(fields[3]).name, fields[2]))
    return out


def main() -> int:
    derived = derived_inputs()
    blocked = False
    checked = 0

    for name, pinned in submodules():
        checkout = ROOT / "repos" / name
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
