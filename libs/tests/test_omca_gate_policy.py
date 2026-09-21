"""Tests for OMCA gate policy evaluator (#461, #109)."""
from __future__ import annotations

import json
from pathlib import Path

from tools.omca_gate_policy import evaluate_audit_report, main


def test_clean_audit_report_passes() -> None:
    report = {
        "target": ".",
        "verdict": "PASS",
        "total_scouts_deployed": 9,
        "overall_score": 1.0,
        "critical_blockers": [],
        "findings": [],
    }
    passed, blocking, warnings = evaluate_audit_report(report)
    assert passed is True
    assert blocking == []
    assert warnings == []


def test_blocked_verdict_fails() -> None:
    report = {
        "target": ".",
        "verdict": "BLOCKED",
        "critical_blockers": ["Found 1 blocker"],
        "findings": [],
    }
    passed, blocking, _ = evaluate_audit_report(report)
    assert passed is False
    assert any("explicitly BLOCKED" in b for b in blocking)


def test_critical_finding_fails() -> None:
    report = {
        "target": ".",
        "verdict": "WARN",
        "findings": [
            {
                "category": "ENGINEERING_BLIND",
                "scout": "G1_INFRA_SRE",
                "topic": "Goroutine Leak Detected",
                "severity": "CRITICAL",
                "details": "Unbounded goroutine creation in worker loop",
                "evidence": "worker.go:42",
            }
        ],
    }
    passed, blocking, _ = evaluate_audit_report(report)
    assert passed is False
    assert any("Goroutine Leak Detected" in b for b in blocking)


def test_ppt_project_finding_blocks() -> None:
    report = {
        "target": ".",
        "verdict": "PASS",
        "findings": [
            {
                "category": "MODULE_CONTRACT",
                "scout": "M2_SPEC_DEVIATION",
                "topic": "PPT Only Project (No Implementation)",
                "severity": "HIGH",
                "details": "Only doc files found, zero source code",
                "evidence": "5 doc files, 0 source files",
            }
        ],
    }
    passed, blocking, _ = evaluate_audit_report(report)
    assert passed is False
    assert any("PPT Only Project" in b for b in blocking)


def test_no_automated_tests_finding_blocks() -> None:
    report = {
        "target": ".",
        "verdict": "PASS",
        "findings": [
            {
                "category": "ENGINEERING_BLIND",
                "scout": "G3_QA_TEST_HOLES",
                "topic": "No Automated Tests Found",
                "severity": "HIGH",
                "details": "Zero test files found in source tree",
                "evidence": "0 test files",
            }
        ],
    }
    passed, blocking, _ = evaluate_audit_report(report)
    assert passed is False
    assert any("No Automated Tests Found" in b for b in blocking)


def test_sha_mismatch_blocks() -> None:
    report = {
        "target": ".",
        "commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "verdict": "PASS",
        "findings": [],
    }
    passed, blocking, _ = evaluate_audit_report(
        report,
        expect_sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )
    assert passed is False
    assert any("SHA mismatch" in b for b in blocking)


def test_non_strict_mode_does_not_block_on_heuristic_topics() -> None:
    report = {
        "target": ".",
        "verdict": "PASS",
        "findings": [
            {
                "category": "MODULE_CONTRACT",
                "scout": "M2_SPEC_DEVIATION",
                "topic": "PPT Only Project (No Implementation)",
                "severity": "HIGH",
                "details": "Only docs found",
                "evidence": "0 source files",
            }
        ],
    }
    # strict=True blocks
    passed_strict, blocking_strict, _ = evaluate_audit_report(report, strict=True)
    assert passed_strict is False
    assert len(blocking_strict) == 1

    # strict=False allows HIGH severity heuristic topics to pass
    passed_lenient, blocking_lenient, warnings = evaluate_audit_report(report, strict=False)
    assert passed_lenient is True
    assert blocking_lenient == []
    assert len(warnings) == 1


def test_main_cli_exit_codes(tmp_path: Path) -> None:
    clean_report = tmp_path / "clean.json"
    clean_report.write_text(json.dumps({"verdict": "PASS", "findings": []}))

    import sys
    orig_argv = sys.argv
    try:
        sys.argv = ["omca_gate_policy", str(clean_report)]
        assert main() == 0

        bad_report = tmp_path / "bad.json"
        bad_report.write_text(json.dumps({
            "verdict": "BLOCKED",
            "findings": [{"severity": "CRITICAL", "topic": "Crash"}],
        }))
        sys.argv = ["omca_gate_policy", str(bad_report)]
        assert main() == 1
    finally:
        sys.argv = orig_argv
