#!/usr/bin/env python3
"""Print the compiled compose of a stack, or run the PR-time equivalence check (#1822).

Usage:
  python -m tools.compose_compile print <compose path> [--mode preview] [--vault-template FILE]
  python -m tools.compose_compile check --base <git ref>
  python -m tools.compose_compile validate <compose path>...

`print` writes the text that the deploy uploads and hashes to stdout. It writes the
Dokploy source type and each raw-eligibility reason to stderr. A plain file prints
its own bytes. `check` exits 1 when a plain file changes in the compile or a migrated
x-infra2 file is not equivalent to its hand-written file at the base ref.
`validate` runs `docker compose config` on each file: a plain file as its source, an
x-infra2 file as its compiled text (common.deploy.compose_validate, #1884).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.deploy.compose_compile import (  # noqa: E402
    MODES,
    ComposeSourceError,
    StackCompose,
    compile_stack,
    has_x_infra2,
    raw_ineligibility,
    vault_template_for,
)


def _print(args: argparse.Namespace) -> int:
    path = Path(args.path)
    text = path.read_text(encoding="utf-8")
    template = (
        Path(args.vault_template).read_text(encoding="utf-8")
        if args.vault_template
        else vault_template_for(path, text)
    )
    try:
        compiled = compile_stack(text, vault_template=template, mode=args.mode)
        stack = StackCompose(
            source=text, compiled=compiled, x_infra2=has_x_infra2(text)
        )
        sys.stdout.write(compiled)
        print(f"# source type: {stack.source_type}", file=sys.stderr)
    except ComposeSourceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    for reason in raw_ineligibility(compiled):
        print(f"# not raw-eligible: {reason}", file=sys.stderr)
    return 0


def _check(args: argparse.Namespace) -> int:
    from common.deploy.compose_equivalence import check

    report = check(args.base)
    print(report.summary())
    for problem in report.problems:
        print(f"ERROR: {problem}")
    return 1 if report.problems else 0


def _validate(args: argparse.Namespace) -> int:
    from common.deploy.compose_validate import validate

    failed = validate(args.paths)
    for path in failed:
        print(f"ERROR: invalid compose configuration: {path}", file=sys.stderr)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_print = sub.add_parser("print", help="print the compiled compose of one file")
    p_print.add_argument("path")
    p_print.add_argument("--mode", choices=MODES, default="deploy")
    p_print.add_argument("--vault-template", default=None)
    p_check = sub.add_parser("check", help="PR-time equivalence against a base ref")
    p_check.add_argument("--base", required=True)
    p_validate = sub.add_parser("validate", help="docker compose config per file")
    p_validate.add_argument("paths", nargs="+")
    args = parser.parse_args(argv)
    commands = {"print": _print, "check": _check, "validate": _validate}
    return commands[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
