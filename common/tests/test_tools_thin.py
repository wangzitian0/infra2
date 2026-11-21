"""`tools/` is a thin layer (docs/ssot/core.harness.md §5.3, infra2#1823 Step 4.1).

A tool parses argv, wires the environment and returns an exit code. Its logic lives
in `common/`, where `common/tests` covers it. This test reads the AST of each
`tools/*.py` and applies two limits:

1. the module defines no top-level class;
2. the module has at most `MAX_CODE_LINES` lines of top-level statements (the module
   docstring is not counted).

`DEBT` lists each tool that broke a limit when this test was added. The ledger can only
shrink: a tool not in `DEBT` that breaks a limit fails, a `DEBT` entry whose tool now
obeys both limits fails, and a tool that grows above its recorded size fails.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
MAX_CODE_LINES = 200

# tool file name -> (top-level classes, code lines) measured on 2026-11-22 at
# infra2@907355ac. Step 4.3 of infra2#1823 moves the logic of each entry to
# `common/`. `facet_reconcile.py` and `host_hygiene_schedule.py` are production
# paths (AGENTS.md merge gate rule 6): the owner approves their edits.
DEBT: dict[str, tuple[int, int]] = {
    "reconcile_iac_inputs.py": (7, 3759),
    "out_of_band_watchdog.py": (4, 2117),
    "ci_runner.py": (0, 865),
    "canary_verify.py": (3, 846),
    "infra_probe_runner.py": (1, 774),
    "service_onboard.py": (0, 509),
    "scaffold.py": (0, 453),
    "pr_merge_gate.py": (0, 437),
    "host_hygiene_schedule.py": (0, 406),
    "run_restore_rehearsal.py": (0, 377),
    "signoz_alert_rule_probe.py": (0, 323),
    "observability_roundtrip_probe.py": (2, 322),
    "local_init.py": (0, 294),
    "service_offboard.py": (0, 250),
    "facet_reconcile.py": (1, 213),
}


def measure(source: str) -> tuple[int, int]:
    """Return (top-level classes, lines of top-level statements without the docstring)."""
    body = ast.parse(source).body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
    ):
        if isinstance(body[0].value.value, str):
            body = body[1:]
    classes = sum(isinstance(node, ast.ClassDef) for node in body)
    lines = sum((node.end_lineno or node.lineno) - node.lineno + 1 for node in body)
    return classes, lines


def violates(measured: tuple[int, int]) -> bool:
    classes, lines = measured
    return classes > 0 or lines > MAX_CODE_LINES


def current() -> dict[str, tuple[int, int]]:
    return {path.name: measure(path.read_text()) for path in sorted(TOOLS.glob("*.py"))}


def test_measure_counts_classes_and_statement_lines_without_the_docstring():
    source = '"""Doc\nline two."""\nimport os\n\n\nclass A:\n    x = 1\n\n\ndef f():\n    return 1\n'
    assert measure(source) == (1, 5)


def test_measure_counts_a_module_without_a_docstring():
    assert measure("import os\nX = 1\n") == (0, 2)


@pytest.mark.parametrize(
    ("measured", "expected"),
    [((0, MAX_CODE_LINES), False), ((0, MAX_CODE_LINES + 1), True), ((1, 1), True)],
)
def test_violates_applies_both_limits(measured, expected):
    assert violates(measured) is expected


def test_no_tool_outside_the_ledger_breaks_a_limit():
    new = {name: m for name, m in current().items() if name not in DEBT and violates(m)}
    assert new == {}, (
        f"tools/ must stay thin (core.harness.md §5.3); move logic to common/: {new}"
    )


def test_each_ledger_entry_still_exists_and_still_breaks_a_limit():
    tools = current()
    stale = sorted(
        name for name in DEBT if name not in tools or not violates(tools[name])
    )
    assert stale == [], f"remove these paid-off entries from DEBT: {stale}"


def test_no_ledger_entry_grows():
    tools = current()
    grown = {
        name: (DEBT[name], tools[name])
        for name in DEBT
        if name in tools
        and (tools[name][0] > DEBT[name][0] or tools[name][1] > DEBT[name][1])
    }
    assert grown == {}, f"a ledger tool grew; move the new logic to common/: {grown}"
