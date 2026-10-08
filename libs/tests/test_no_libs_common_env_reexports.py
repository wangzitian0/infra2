"""Guard: ``libs.common`` offers ``check_service`` and nothing else (#1164, part of #1059).

``libs.common`` once re-exported the helpers of ``libs.core.environ``. Callers now import
those helpers from ``libs.core.environ``. This guard reads the AST of every tracked
python file. It fails on each of these uses of ``libs.common``:

* ``from libs.common import <name>`` where ``<name>`` is not ``check_service``;
* ``from libs import common`` or ``import libs.common as c`` followed by a read of any
  attribute except ``check_service`` (``c.get_env``, ``libs.common.get_env``);
* ``getattr(c, "get_env")``, ``hasattr(c, "get_env")``, ``monkeypatch.setattr(c, "get_env", f)``
  and ``patch.object(c, "get_env")``;
* a string target such as ``"libs.common.get_env"`` for ``mock.patch`` or
  ``monkeypatch.setattr``. The module has no such attribute, so the patch fails at once.

Relative imports inside ``libs/`` count. Imports inside functions count. Source text is
parsed, never executed, so a module that needs a secret is still checked.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

COMMON = "libs.common"
ALLOWED = frozenset({"check_service"})

# A string that names one attribute of libs.common, for example a mock.patch target.
_DOTTED_TARGET = re.compile(r"libs\.common\.(?P<name>\w+)(?:\..*)?")


def _is_allowed(name: str) -> bool:
    """Return True for ``check_service`` and for module metadata such as ``__all__``."""
    return name in ALLOWED or (name.startswith("__") and name.endswith("__"))


def _package_parts(rel_path: str) -> list[str]:
    """Return the package that holds the file, as dotted-name parts."""
    return list(Path(rel_path).parts[:-1])


def _absolute_module(module: str | None, level: int, package: list[str]) -> str | None:
    """Return the absolute dotted name of an ``ImportFrom``, or None if it leaves the tree."""
    if level == 0:
        return module or ""
    if level - 1 > len(package):
        return None
    base = package[: len(package) - (level - 1)]
    return ".".join([*base, *([module] if module else [])])


def _is_common_module(node: ast.expr, aliases: set[str]) -> bool:
    """Return True when the expression is the ``libs.common`` module."""
    if isinstance(node, ast.Name):
        return node.id in aliases
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "common"
        and isinstance(node.value, ast.Name)
        and node.value.id == "libs"
    )


def libs_common_violations(source: str, rel_path: str = "<memory>.py") -> list[str]:
    """Return one line per use of ``libs.common`` other than ``check_service``."""
    tree = ast.parse(source)
    package = _package_parts(rel_path)
    aliases: set[str] = set()
    found: list[tuple[int, str]] = []

    def report(node: ast.AST, what: str) -> None:
        line = getattr(node, "lineno", 0)
        found.append((line, f"{rel_path}:{line}: {what}"))

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            target = _absolute_module(node.module, node.level, package)
            if target == COMMON:
                for item in node.names:
                    if not _is_allowed(item.name):
                        report(node, f"imports {item.name} from libs.common")
            elif target == "libs":
                aliases.update(
                    item.asname or item.name
                    for item in node.names
                    if item.name == "common"
                )
        elif isinstance(node, ast.Import):
            aliases.update(
                item.asname for item in node.names if item.name == COMMON and item.asname
            )

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if _is_common_module(node.value, aliases) and not _is_allowed(node.attr):
                report(node, f"reads libs.common.{node.attr}")
        elif isinstance(node, ast.Call) and len(node.args) >= 2:
            first, second = node.args[0], node.args[1]
            if (
                _is_common_module(first, aliases)
                and isinstance(second, ast.Constant)
                and isinstance(second.value, str)
                and not _is_allowed(second.value)
            ):
                report(node, f"names libs.common.{second.value} in a call")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            match = _DOTTED_TARGET.fullmatch(node.value)
            if match and not _is_allowed(match["name"]):
                report(node, f"string target {node.value!r}")

    return [message for _line, message in sorted(found)]


def _tracked_python_files() -> list[str]:
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--", "*.py"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split("\0")
    return [name for name in tracked if name and (ROOT / name).is_file()]


def test_no_tracked_python_file_uses_a_former_libs_common_export() -> None:
    files = _tracked_python_files()
    assert len(files) > 100, "the tracked-file scan found almost nothing"
    found = [
        line
        for rel in files
        for line in libs_common_violations(
            (ROOT / rel).read_text(encoding="utf-8"), rel
        )
    ]
    assert found == [], (
        "Import environment helpers from libs.core.environ. "
        "libs.common offers check_service only (#1164):\n" + "\n".join(found)
    )


def test_libs_common_offers_check_service_only() -> None:
    import libs.common as common
    from libs.core import environ

    assert common.__all__ == ["check_service"]
    bound = set(vars(common)) & set(environ.__all__)
    assert bound == set(), f"libs.common binds names of libs.core.environ: {sorted(bound)}"
    assert callable(common.check_service)


# --- Self-tests: the guard must fail on a violation and pass on compliant code. ---------


def test_guard_flags_a_from_import_of_an_environment_helper() -> None:
    source = "from libs.common import check_service, get_env\n"
    assert libs_common_violations(source) == [
        "<memory>.py:1: imports get_env from libs.common"
    ]


def test_guard_flags_a_multi_line_import_and_an_import_inside_a_function() -> None:
    source = (
        "from libs.common import (\n"
        "    check_service,\n"
        "    SERVICE_SUBDOMAINS as subdomains,\n"
        ")\n"
        "\n"
        "def f():\n"
        "    from libs.common import with_env_suffix\n"
    )
    assert libs_common_violations(source) == [
        "<memory>.py:1: imports SERVICE_SUBDOMAINS from libs.common",
        "<memory>.py:7: imports with_env_suffix from libs.common",
    ]


def test_guard_flags_a_star_import() -> None:
    assert libs_common_violations("from libs.common import *\n") == [
        "<memory>.py:1: imports * from libs.common"
    ]


def test_guard_flags_attribute_reads_through_every_alias_form() -> None:
    source = (
        "import libs.common as c\n"
        "from libs import common\n"
        "from libs import common as m\n"
        "import libs.common\n"
        "c.get_env()\n"
        "common.infra_domain()\n"
        "m.SERVICE_SUBDOMAINS\n"
        "libs.common.validate_env()\n"
        "c.check_service\n"
        "c.__all__\n"
    )
    assert libs_common_violations(source) == [
        "<memory>.py:5: reads libs.common.get_env",
        "<memory>.py:6: reads libs.common.infra_domain",
        "<memory>.py:7: reads libs.common.SERVICE_SUBDOMAINS",
        "<memory>.py:8: reads libs.common.validate_env",
    ]


def test_guard_flags_attribute_names_given_as_strings() -> None:
    source = (
        "from libs import common\n"
        "hasattr(common, 'DeploymentEnvironment')\n"
        "monkeypatch.setattr(common, 'get_env', f)\n"
        "patch.object(common, 'check_service')\n"
    )
    assert libs_common_violations(source) == [
        "<memory>.py:2: names libs.common.DeploymentEnvironment in a call",
        "<memory>.py:3: names libs.common.get_env in a call",
    ]


def test_guard_flags_a_string_patch_target() -> None:
    source = (
        "monkeypatch.setattr('libs.common.get_env', f)\n"
        "mock.patch('libs.common.check_service')\n"
        "'libs.common.not_a_target is prose, not a target'\n"
    )
    assert libs_common_violations(source) == [
        "<memory>.py:1: string target 'libs.common.get_env'"
    ]


def test_guard_flags_relative_imports_that_reach_libs_common() -> None:
    source = (
        "from . import common\n"
        "from .common import get_env\n"
        "common.get_env\n"
    )
    assert libs_common_violations(source, "libs/x.py") == [
        "libs/x.py:2: imports get_env from libs.common",
        "libs/x.py:3: reads libs.common.get_env",
    ]


def test_guard_resolves_parent_relative_imports_from_a_subpackage() -> None:
    source = (
        "from .. import common as up\n"
        "from ..common import get_env\n"
        "from . import common\n"
        "up.service_domain\n"
        "common.get_env\n"
    )
    # Line 3 names libs.core.common, a different module, so line 5 is not a violation.
    assert libs_common_violations(source, "libs/core/x.py") == [
        "libs/core/x.py:2: imports get_env from libs.common",
        "libs/core/x.py:4: reads libs.common.service_domain",
    ]


def test_guard_passes_compliant_code() -> None:
    source = (
        "from libs.common import check_service\n"
        "from libs.core.environ import get_env, infra_domain\n"
        "from libs import common\n"
        "import libs.common as c\n"
        "\n"
        "def f(ctx):\n"
        "    from libs.common import check_service as health\n"
        "    common.check_service(ctx, 'redis', 'true')\n"
        "    c.check_service\n"
        "    patch('libs.common.check_service')\n"
        "    return get_env(), infra_domain(), health\n"
    )
    assert libs_common_violations(source) == []


def test_guard_ignores_a_relative_import_of_another_module() -> None:
    source = "from .environ import get_env\nfrom . import environ\n"
    assert libs_common_violations(source, "libs/core/x.py") == []
