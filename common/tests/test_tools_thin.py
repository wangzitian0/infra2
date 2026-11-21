"""`tools/` is a thin layer (docs/ssot/core.harness.md §5.3, infra2#1823 Step 4.1).

A tool parses argv, wires the environment and returns an exit code. Its logic lives
in `common/`, where `common/tests` covers it. This test reads the AST of each `.py`
file under `tools/` and applies two limits:

1. the module defines no top-level class;
2. the module has at most `MAX_CODE_LINES` lines of top-level statements, decorators
   included and the module docstring excluded.

`DEBT` lists each tool that broke a limit when this test was added, with its exact
measured size. The ledger is a ratchet:

- a tool not in `DEBT` that breaks a limit fails;
- a `DEBT` entry whose tool is gone or now obeys both limits fails (delete the entry);
- a `DEBT` entry whose recorded size differs from the measured size fails, so a tool
  that grows fails and a tool that shrinks must lower its recorded size.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
MAX_CODE_LINES = 200
#: `tools/` held 65 `.py` files on 2026-11-22. A scan that finds fewer than this has
#: lost the tree (wrong path or an empty glob) and would pass while checking nothing.
MIN_SCANNED_TOOLS = 40

Size = tuple[int, int]

# tool path relative to tools/ -> (top-level classes, code lines), measured on
# 2026-11-22 at infra2@907355ac. Step 4.3 of infra2#1823 moves the logic of each
# entry to `common/`. `facet_reconcile.py` and `host_hygiene_schedule.py` are
# production paths (AGENTS.md merge gate rule 6): the owner approves their edits.
DEBT: dict[str, Size] = {
    "reconcile_iac_inputs.py": (7, 3764),
    "out_of_band_watchdog.py": (4, 2121),
    "ci_runner.py": (0, 865),
    "canary_verify.py": (3, 848),
    "infra_probe_runner.py": (1, 774),
    "service_onboard.py": (0, 510),
    "scaffold.py": (0, 453),
    "pr_merge_gate.py": (0, 437),
    "host_hygiene_schedule.py": (0, 406),
    "run_restore_rehearsal.py": (0, 377),
    "observability_roundtrip_probe.py": (2, 323),
    "signoz_alert_rule_probe.py": (0, 323),
    "local_init.py": (0, 299),
    "service_offboard.py": (0, 251),
    "facet_reconcile.py": (1, 214),
}


def measure(source: str) -> Size:
    """Return (top-level classes, lines of top-level statements without the docstring)."""
    body = ast.parse(source).body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    classes = sum(isinstance(node, ast.ClassDef) for node in body)
    lines = 0
    for node in body:
        first = min(
            [node.lineno, *(d.lineno for d in getattr(node, "decorator_list", []))]
        )
        lines += (node.end_lineno or node.lineno) - first + 1
    return classes, lines


def violates(size: Size) -> bool:
    classes, lines = size
    return classes > 0 or lines > MAX_CODE_LINES


def ledger_problems(
    tools: dict[str, Size], debt: dict[str, Size]
) -> list[tuple[str, str]]:
    """Return (kind, tool) for each way `tools` and `debt` break the ratchet.

    `new`: move the tool's logic to common/. `paid`: delete the DEBT entry.
    `changed`: record the measured size in DEBT (a growth also means: move logic out).
    """
    problems = [
        ("new", name)
        for name, size in sorted(tools.items())
        if name not in debt and violates(size)
    ]
    for name, recorded in sorted(debt.items()):
        if name not in tools or not violates(tools[name]):
            problems.append(("paid", name))
        elif tools[name] != recorded:
            problems.append(("changed", name))
    return problems


def current() -> dict[str, Size]:
    return {
        path.relative_to(TOOLS).as_posix(): measure(path.read_text())
        for path in sorted(TOOLS.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def test_measure_counts_classes_and_statement_lines_without_the_docstring():
    source = '"""Doc\nline two."""\nimport os\n\n\nclass A:\n    x = 1\n\n\ndef f():\n    return 1\n'
    assert measure(source) == (1, 5)


def test_measure_counts_a_module_without_a_docstring():
    assert measure("import os\nX = 1\n") == (0, 2)


def test_measure_counts_decorator_lines():
    assert measure("@a\n@b\ndef f():\n    return 1\n") == (0, 4)


@pytest.mark.parametrize(
    ("size", "expected"),
    [((0, MAX_CODE_LINES), False), ((0, MAX_CODE_LINES + 1), True), ((1, 1), True)],
)
def test_violates_applies_both_limits(size, expected):
    assert violates(size) is expected


FAT = (0, MAX_CODE_LINES + 10)


@pytest.mark.parametrize(
    ("tools", "debt", "expected"),
    [
        ({"a.py": (0, 10)}, {}, []),
        ({"a.py": FAT}, {}, [("new", "a.py")]),
        ({"a.py": FAT}, {"a.py": FAT}, []),
        ({"a.py": (0, 10)}, {"a.py": FAT}, [("paid", "a.py")]),
        ({}, {"a.py": FAT}, [("paid", "a.py")]),
        ({"a.py": (0, FAT[1] + 1)}, {"a.py": FAT}, [("changed", "a.py")]),
        ({"a.py": (0, FAT[1] - 1)}, {"a.py": FAT}, [("changed", "a.py")]),
        ({"a.py": (1, FAT[1])}, {"a.py": FAT}, [("changed", "a.py")]),
    ],
)
def test_ledger_problems_on_synthetic_trees(tools, debt, expected):
    assert ledger_problems(tools, debt) == expected


def test_the_scan_sees_the_real_tools_tree():
    assert len(current()) >= MIN_SCANNED_TOOLS


def test_tools_tree_obeys_the_ledger():
    tools = current()
    problems = [
        (kind, name, DEBT.get(name), tools.get(name))
        for kind, name in ledger_problems(tools, DEBT)
    ]
    assert problems == [], f"tools/ must stay thin (core.harness.md §5.3): {problems}"
