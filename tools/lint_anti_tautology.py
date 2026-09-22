"""AST static analysis tool to detect tautological / fake assertions in test suites (#806).

Enforces Apocalypse Rule 7 (Testing Fidelity / Anti-Puppet & Anti-Tautology):
- assert True (or constant truthy asserts)
- assert ... or True (or truthy literal in disjunction)
- not in ...[:0] (tautological slice)
- not in empty literals ([], (), {}, "", b"")
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


class TautologyVisitor(ast.NodeVisitor):
    """AST visitor that detects tautological assertion patterns."""

    def __init__(self, filename: str = "<input>") -> None:
        self.filename = filename
        self.violations: list[str] = []

    def visit_Assert(self, node: ast.Assert) -> None:
        # 1. Direct constant truthy assert: assert True, assert 1, assert "foo"
        if isinstance(node.test, ast.Constant) and bool(node.test.value):
            self.violations.append(
                f"{self.filename}:{node.lineno}: Tautological assertion: 'assert {node.test.value!r}' is unconditionally true."
            )

        # Inspect expressions inside the assert test
        for child in ast.walk(node.test):
            # 2. assert ... or True (or truthy literal in disjunction)
            if isinstance(child, ast.BoolOp) and isinstance(child.op, ast.Or):
                for val in child.values:
                    if isinstance(val, ast.Constant) and bool(val.value):
                        self.violations.append(
                            f"{self.filename}:{child.lineno}: Tautological disjunction: 'or {val.value!r}' short-circuits assertion."
                        )

            # 3. not in ...[:0] or not in empty literals
            if isinstance(child, ast.Compare):
                for op, comparator in zip(child.ops, child.comparators):
                    if isinstance(op, ast.NotIn):
                        # check [:0] or [0:0]
                        if isinstance(comparator, ast.Subscript):
                            sl = comparator.slice
                            if isinstance(sl, ast.Slice):
                                if (
                                    isinstance(sl.upper, ast.Constant)
                                    and sl.upper.value == 0
                                    and (
                                        sl.lower is None
                                        or (
                                            isinstance(sl.lower, ast.Constant)
                                            and sl.lower.value == 0
                                        )
                                    )
                                ):
                                    self.violations.append(
                                        f"{self.filename}:{child.lineno}: Tautological comparison: 'not in ...[:0]' is always true."
                                    )
                        # check empty sequence / set / dict literals
                        elif isinstance(comparator, (ast.List, ast.Tuple, ast.Set)) and not comparator.elts:
                            self.violations.append(
                                f"{self.filename}:{child.lineno}: Tautological comparison: 'not in empty literal container' is always true."
                            )
                        elif isinstance(comparator, ast.Dict) and not comparator.keys:
                            self.violations.append(
                                f"{self.filename}:{child.lineno}: Tautological comparison: 'not in empty dict' is always true."
                            )
                        elif isinstance(comparator, ast.Constant) and comparator.value in ("", b""):
                            self.violations.append(
                                f"{self.filename}:{child.lineno}: Tautological comparison: 'not in empty string literal' is always true."
                            )

        self.generic_visit(node)


def check_source(code: str, filename: str = "<input>") -> list[str]:
    """Parse source code and return a list of violation messages."""
    try:
        tree = ast.parse(code, filename=filename)
    except SyntaxError as exc:
        return [f"{filename}:{exc.lineno}: SyntaxError: {exc.msg}"]
    visitor = TautologyVisitor(filename)
    visitor.visit(tree)
    return visitor.violations


def check_file(path: Path) -> list[str]:
    """Check a single Python file for tautological assertions."""
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return [f"{path}: Failed to read file: {exc}"]
    return check_source(content, str(path))


def check_paths(paths: list[str | Path]) -> list[str]:
    """Recursively check given files and directories."""
    all_violations: list[str] = []
    for raw in paths:
        p = Path(raw)
        if p.is_file() and p.suffix == ".py":
            all_violations.extend(check_file(p))
        elif p.is_dir():
            for f in sorted(p.rglob("*.py")):
                all_violations.extend(check_file(f))
        elif not p.exists():
            all_violations.append(f"{p}: Path does not exist")
    return all_violations


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("Usage: python tools/lint_anti_tautology.py <file-or-dir> ...")
        return 0

    violations = check_paths(args)
    if violations:
        print(f"Anti-tautology lint failed! Found {len(violations)} violation(s):")
        for v in violations:
            print(f"  ERROR: {v}")
        return 1

    print(f"Anti-tautology lint passed: checked {len(args)} target(s) cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
