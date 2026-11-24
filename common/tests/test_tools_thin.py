"""`tools/` is a thin layer (docs/ssot/core.harness.md §5.3, infra2#1823 Step 4.1).

A tool parses argv, wires the environment and returns an exit code. Its logic lives
in `common/`, where `common/tests` covers it. This test reads the AST of each `.py`
file under `tools/` and applies two limits:

1. the module defines no top-level class;
2. the module has at most `MAX_CODE_LINES` lines of top-level statements, decorators
   included and the module docstring excluded.

No tool is exempt. A tool that breaks a limit fails the test: move its logic to `common/`.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
MAX_CODE_LINES = 200
#: `tools/` held 65 `.py` files on 2026-11-22. A scan that finds fewer than this has
#: lost the tree (wrong path or an empty glob) and would pass while checking nothing.
MIN_SCANNED_TOOLS = 40


def measure(source: str) -> tuple[int, int]:
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


def test_tools_tree_is_thin() -> None:
    # The measure counts a decorator line and skips the docstring.
    assert measure('"""Doc."""\n@a\n@b\nclass A:\n    x = 1\n') == (1, 4)
    sizes = {
        path.relative_to(TOOLS).as_posix(): measure(path.read_text())
        for path in sorted(TOOLS.rglob("*.py"))
        if "__pycache__" not in path.parts
    }
    assert len(sizes) >= MIN_SCANNED_TOOLS, "the scan lost the tools/ tree"
    fat = {
        name: size
        for name, size in sizes.items()
        if size[0] > 0 or size[1] > MAX_CODE_LINES
    }
    assert fat == {}, f"tools/ must stay thin (core.harness.md §5.3): {fat}"
