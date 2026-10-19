"""Fail-closed CI policy evaluator for OMCA 3-Category 4+3+2=9 Doomsday Audits."""

from __future__ import annotations

from typing import Any

BLOCKING_SEVERITIES = frozenset({"BLOCKER", "CRITICAL"})
BLOCKING_TOPICS = frozenset(
    {
        "ppt only project (no implementation)",
        "no automated tests found",
    }
)

__all__ = [
    "BLOCKING_SEVERITIES",
    "BLOCKING_TOPICS",
    "evaluate_audit_report",
]


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

    if expect_sha:
        report_sha = report.get("commit") or report.get("head_sha") or report.get("sha")
        if report_sha and report_sha != expect_sha:
            blocking.append(
                f"Audit report SHA mismatch: expected {expect_sha[:10]}, found {report_sha[:10]}"
            )

    verdict = str(report.get("verdict") or "").upper()
    if verdict == "BLOCKED":
        blocking.append("OMCA audit verdict is explicitly BLOCKED")

    critical_blockers = report.get("critical_blockers") or []
    for cb in critical_blockers:
        blocking.append(f"Critical Blocker: {cb}")

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

        if severity in BLOCKING_SEVERITIES:
            blocking.append(msg)
            continue

        if (
            strict
            and topic.lower() in BLOCKING_TOPICS
            and severity in ("HIGH", "CRITICAL", "BLOCKER")
        ):
            blocking.append(f"FATAL ARCHITECTURAL DEFECT: {msg}")
            continue

        if severity in ("HIGH", "WARN", "WARNING"):
            warnings.append(msg)

    deduped_blocking = list(dict.fromkeys(blocking))
    passed = len(deduped_blocking) == 0

    return passed, deduped_blocking, warnings
