#!/usr/bin/env python3
"""Fail-closed CI policy evaluator for OMCA 3-Category 4+3+2=9 Doomsday Audits.

Parses the JSON output produced by `omca audit --json` and enforces merge gate blocking rules:
- Exit 0: Audit passed or only advisory findings present.
- Exit 1: BLOCKER or CRITICAL finding detected (fail-closed gate obstruction).
- Exit 2: Malformed input, missing audit report, or unhandled infrastructure error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from common.gate.omca_policy import (
    BLOCKING_SEVERITIES,
    BLOCKING_TOPICS,
    evaluate_audit_report,
)

__all__ = [
    "BLOCKING_SEVERITIES",
    "BLOCKING_TOPICS",
    "evaluate_audit_report",
    "main",
]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate OMCA audit report against CI merge gate policy."
    )
    parser.add_argument(
        "report_file",
        nargs="?",
        type=Path,
        default=None,
        help="Path to audit.json file or '-' for stdin",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run built-in policy evaluation self-test",
    )
    parser.add_argument(
        "--expect-sha", type=str, default=None, help="Expected head commit SHA"
    )
    parser.add_argument(
        "--no-strict",
        dest="strict",
        action="store_false",
        default=True,
        help="Do not block on architectural heuristic topics, only on explicit CRITICAL/BLOCKER severities",
    )
    args = parser.parse_args()

    if args.self_test:
        clean_res, _, _ = evaluate_audit_report(
            {"verdict": "PASS", "findings": []}, strict=True
        )
        blocked_res, _, _ = evaluate_audit_report(
            {"verdict": "BLOCKED", "critical_blockers": ["synthetic"]}, strict=True
        )
        if clean_res and not blocked_res:
            print(
                "omca_gate_policy: Self-test passed (clean report passed, blocker correctly blocked)"
            )
            return 0
        print("omca_gate_policy ERROR: Self-test assertion failed", file=sys.stderr)
        return 1

    if not args.report_file:
        parser.print_help(sys.stderr)
        return 2

    try:
        if str(args.report_file) == "-":
            raw_data = sys.stdin.read()
        else:
            if not args.report_file.is_file():
                print(
                    f"omca_gate_policy ERROR: Report file not found: {args.report_file}",
                    file=sys.stderr,
                )
                return 2
            raw_data = args.report_file.read_text(encoding="utf-8")

        report = json.loads(raw_data)
    except Exception as exc:
        print(
            f"omca_gate_policy ERROR: Failed to parse audit JSON: {exc}",
            file=sys.stderr,
        )
        return 2

    passed, blocking, warnings = evaluate_audit_report(
        report,
        expect_sha=args.expect_sha,
        strict=args.strict,
    )

    if warnings:
        print("::warning:: OMCA Audit Warnings:", file=sys.stderr)
        for w in warnings:
            print(f"  [WARN] {w}", file=sys.stderr)

    if not passed:
        print("omca_gate_policy: GATE BLOCKED by OMCA findings:", file=sys.stderr)
        for b in blocking:
            print(f"  ::error:: [BLOCKER] {b}", file=sys.stderr)
        return 1

    print("omca_gate_policy: PASS (zero blocking findings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
