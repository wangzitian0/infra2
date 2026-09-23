"""The `libs/README.md` shim table is the contract; this file is what enforces it.

#846: the table declared nine flat modules "Frozen Shim" while six of them still held
the implementation and the domain packages reverse-imported them. Anyone who followed
the README to `libs/security/` found a 49-line shell. A table nobody checks drifts the
moment the code moves, and the reader who trusted it is the one who ends up wrong.

Two directions are guarded, because drift happens both ways:

* a row marked `Frozen Shim` must BE one — structurally a re-export of exactly the
  module the table names, with every re-exported name the **same object** on the old
  path and the new one. Identity (`is`), not `callable()`: a shim that quietly grew its
  own copy of a symbol passes every truthiness check and diverges on the next edit;
* a row marked otherwise must NOT be one — so migrating it forces the table to move
  with it, instead of leaving the next reader a stale claim.

The old-path imports in `test_infra_probes.py`, `test_container_breakdown.py`,
`test_backup_verification.py`, `test_secrets_supply.py` and `test_watchdog_issue_trail.py`
are the other half of the evidence: they exercise the shims the way real callers do.
Deleting either half leaves the shims unverified.
"""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "libs" / "README.md"

_SHIM_SECTION = "## 🛡️ Backward-Compatibility Shims"
_CODE = re.compile(r"`([^`]+)`")


def _shim_table() -> list[tuple[str, str, str]]:
    """(module path, implementation module or "", status) for every table row."""
    text = README.read_text(encoding="utf-8")
    start = text.index(_SHIM_SECTION)
    rows: list[tuple[str, str, str]] = []
    for line in text[start:].splitlines():
        if line.startswith("## ") and _SHIM_SECTION not in line:
            break
        if not line.startswith("| `libs/"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        assert len(cells) == 4, f"unexpected shim-table row shape: {line}"
        shim_cell, target_cell, _symbols, status = cells
        shim = _CODE.search(shim_cell).group(1)
        target_match = _CODE.search(target_cell)
        rows.append((shim, target_match.group(1) if target_match else "", status))
    return rows


TABLE = _shim_table()
FROZEN = [(s, t) for s, t, status in TABLE if status == "Frozen Shim"]
NOT_FROZEN = [(s, status) for s, _t, status in TABLE if status != "Frozen Shim"]


def _module_name(relative_path: str) -> str:
    return relative_path.removesuffix(".py").replace("/", ".")


def _is_pure_reexport(path: Path) -> tuple[bool, str]:
    """True when the module's top level is docstring + one libs import + __all__."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported_from: list[str] = []
    saw_all = False
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # module docstring
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "__future__":
                continue
            if not module.startswith("libs."):
                return False, f"imports {module!r}, which is not a domain module"
            imported_from.append(module)
            continue
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "__all__"
        ):
            saw_all = True
            continue
        return False, f"holds a top-level {type(node).__name__} — not a re-export"
    if not saw_all:
        return False, "declares no __all__"
    if len(imported_from) != 1:
        return False, f"re-exports from {len(imported_from)} modules: {imported_from}"
    return True, imported_from[0]


def test_the_shim_table_is_not_empty() -> None:
    """A parser that silently matches nothing would pass every test below."""
    assert len(TABLE) >= 10, TABLE
    assert len(FROZEN) >= 7, FROZEN
    assert NOT_FROZEN, "expected the table to still list the un-migrated modules"


@pytest.mark.parametrize(("shim", "target"), FROZEN, ids=[s for s, _ in FROZEN])
def test_frozen_shim_holds_only_re_exports_of_the_module_the_table_names(
    shim: str, target: str
) -> None:
    path = ROOT / shim
    assert path.exists(), f"{shim} is in libs/README.md but not on disk"

    pure, detail = _is_pure_reexport(path)
    assert pure, (
        f"{shim} is marked 'Frozen Shim' in libs/README.md but {detail}. Move the "
        f"implementation into {target or 'its domain package'} and leave only "
        f"re-exports here (#846)."
    )
    assert detail == target, (
        f"libs/README.md says {shim} re-exports {target}, but it actually re-exports "
        f"{detail} — the table and the code disagree."
    )


@pytest.mark.parametrize(("shim", "target"), FROZEN, ids=[s for s, _ in FROZEN])
def test_every_re_exported_name_is_the_same_object_on_both_paths(
    shim: str, target: str
) -> None:
    """The shim exists so the OLD import keeps resolving — to the very same object."""
    old = importlib.import_module(_module_name(shim))
    new = importlib.import_module(target)

    exported = list(getattr(old, "__all__"))
    assert exported, f"{shim} re-exports nothing"
    for name in exported:
        assert hasattr(old, name), f"{shim}.__all__ names {name!r}, which is absent"
        assert hasattr(new, name), f"{target} does not define {name!r}"
        assert getattr(old, name) is getattr(new, name), (
            f"{_module_name(shim)}.{name} is not the same object as {target}.{name} — "
            "the shim has grown its own copy"
        )


@pytest.mark.parametrize(
    ("module", "status"), NOT_FROZEN, ids=[m for m, _ in NOT_FROZEN]
)
def test_modules_the_table_calls_implementations_really_are(
    module: str, status: str
) -> None:
    """The other direction: finish migrating one and the table must move with it."""
    pure, _detail = _is_pure_reexport(ROOT / module)
    assert not pure, (
        f"{module} is now a pure re-export, but libs/README.md still lists it as "
        f"{status!r}. Move its row up into the Frozen Shim rows with the module that "
        "now holds the implementation (#846)."
    )
