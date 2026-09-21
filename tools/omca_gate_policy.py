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
from typing import Any

BLOCKING_SEVERITIES = frozenset({"BLOCKER", "CRITICAL"})
BLOCKING_TOPICS = frozenset({
    "ppt only project (no implementation)",
    "no automated tests found",
})


def evaluate_audit_report(
    report: dict[str, Any],
    *,
    expect_sha: str | None = None,
    strict: bool = True,
) -> tuple[bool, list[str], list[str]]:
    """Evaluates an OMCA audit report.

    Returns:
        (passed, blocking_reasons, warning_messages)
    """
    blocking: list[str] = []
    warnings: list[str] = []

    # Optional SHA binding check (prevents stale / tampered reports)
    if expect_sha:
        report_sha = report.get("commit") or report.get("head_sha") or report.get("sha")
        if report_sha and report_sha != expect_sha:
            blocking.append(
                f"Audit report SHA mismatch: expected {expect_sha[:10]}, found {report_sha[:10]}"
            )

    # 1. Check top-level verdict
    verdict = str(report.get("verdict") or "").upper()
    if verdict == "BLOCKED":
        blocking.append("OMCA audit verdict is explicitly BLOCKED")

    # 2. Check explicit critical blockers from report synthesis
    critical_blockers = report.get("critical_blockers") or []
    for cb in critical_blockers:
        blocking.append(f"Critical Blocker: {cb}")

    # 3. Check findings collection
    findings = report.get("findings") or []
    for f in findings:
        if not isinstance(f, dict):
            continue
        scout = f.get("scout") or "UNKNOWN_SCOUT"
        topic = str(f.get("topic") or "")
        severity = str(f.get("severity") or "").upper()
        details = f.get("details") or ""
        evidence = f.get("evidence") or ""

        msg = f"[{scout}] {topic}: {details} (Evidence: {evidence})"

        # Check blocking severities
        if severity in BLOCKING_SEVERITIES:
            blocking.append(msg)
            continue

        # Check fatal topics when in strict mode (e.g. PPT Project / Zero Tests)
        if strict and topic.lower() in BLOCKING_TOPICS and severity in ("HIGH", "CRITICAL", "BLOCKER"):
            blocking.append(f"FATAL ARCHITECTURAL DEFECT: {msg}")
            continue

        if severity in ("HIGH", "WARN", "WARNING"):
            warnings.append(msg)

    # Deduplicate blocking reasons while preserving order
    deduped_blocking = list(dict.fromkeys(blocking))
    passed = len(deduped_blocking) == 0

    return passed, deduped_blocking, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate OMCA audit report against CI merge gate policy.")
    parser.add_argument("report_file", type=Path, help="Path to audit.json file or '-' for stdin")
    parser.add_argument("--expect-sha", type=str, default=None, help="Expected head commit SHA")
    parser.add_argument(
        "--no-strict",
        dest="strict",
        action="store_false",
        default=True,
        help="Do not block on architectural heuristic topics, only on explicit CRITICAL/BLOCKER severities",
    )
    args = parser.parse_args()

    try:
        if str(args.report_file) == "-":
            raw_data = sys.stdin.read()
        else:
            if not args.report_file.is_file():
                print(f"omca_gate_policy ERROR: Report file not found: {args.report_file}", file=sys.stderr)
                return 2
            raw_data = args.report_file.read_text(encoding="utf-8")

        report = json.loads(raw_data)
    except Exception as exc:
        print(f"omca_gate_policy ERROR: Failed to parse audit JSON: {exc}", file=sys.stderr)
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
