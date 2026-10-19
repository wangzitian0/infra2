"""Guard: ``common.common`` offers ``check_service`` and nothing else (#1164, part of #1059).

``common.common`` once re-exported the helpers of ``common.core.environ``. Callers now import
those helpers from ``common.core.environ``. This guard reads the AST of every tracked
python file. It fails on each of these uses of ``common.common``:

* ``from common.common import <name>`` where ``<name>`` is not ``check_service``;
* ``from common import common`` or ``import common.common as c`` followed by a read of any
  attribute except ``check_service`` (``c.get_env``, ``common.common.get_env``);
* ``getattr(c, "get_env")``, ``hasattr(c, "get_env")``, ``monkeypatch.setattr(c, "get_env", f)``
  and ``patch.object(c, "get_env")``;
* a string target such as ``"common.common.get_env"`` for ``mock.patch`` or
  ``monkeypatch.setattr``. The module has no such attribute, so the patch fails at once;
* a name bound at module level in ``common/common.py`` that is not in ``ALLOWED_MODULE_NAMES``.
  The AST of the file gives these names, so private names such as
  ``_REGISTRY_BACKED_SHORT_NAMES`` count too.

Relative imports inside ``common/`` count. Imports inside functions count. Source text is
parsed, never executed, so a module that needs a secret is still checked.

Known residual, accepted: the guard does not read these forms. Each raises at first run.
They are ``importlib.import_module("common.common")``, ``sys.modules["common.common"]``,
``patch.multiple`` and a string target that an f-string builds.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

COMMON = "common.common"
ALLOWED = frozenset({"check_service"})

# Every name that common/common.py binds at module level, except dunder names.
# ``check_service`` is the function. The rest are the imports that the file needs.
ALLOWED_MODULE_NAMES = frozenset(
    {"TYPE_CHECKING", "annotations", "check_service", "environ", "Context", "shlex"}
)

# A string that names one attribute of common.common, for example a mock.patch target.
_DOTTED_TARGET = re.compile(r"common\.common\.(?P<name>\w+)(?:\..*)?")


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


def _is_common_module(
    node: ast.expr, aliases: set[str], has_import_common_common: bool = False
) -> bool:
    """Return True when the expression is the ``common.common`` module."""
    if isinstance(node, ast.Name):
        return node.id in aliases
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "common"
        and isinstance(node.value, ast.Name)
        and node.value.id == "common"
        and (has_import_common_common or "common" not in aliases)
    )


def libs_common_violations(source: str, rel_path: str = "<memory>.py") -> list[str]:
    """Return one line per use of ``common.common`` other than ``check_service``."""
    tree = ast.parse(source)
    package = _package_parts(rel_path)
    aliases: set[str] = set()
    has_import_common_common = False
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
                        report(node, f"imports {item.name} from common.common")
            elif target == "common":
                aliases.update(
                    item.asname or item.name
                    for item in node.names
                    if item.name == "common"
                )
        elif isinstance(node, ast.Import):
            for item in node.names:
                if item.name == COMMON:
                    if item.asname:
                        aliases.add(item.asname)
                    else:
                        has_import_common_common = True

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if (
                node.attr == "common"
                and isinstance(node.value, ast.Name)
                and node.value.id == "common"
                and has_import_common_common
            ):
                continue
            if _is_common_module(
                node.value, aliases, has_import_common_common
            ) and not _is_allowed(node.attr):
                report(node, f"reads common.common.{node.attr}")
        elif isinstance(node, ast.Call) and len(node.args) >= 2:
            first, second = node.args[0], node.args[1]
            if (
                _is_common_module(first, aliases, has_import_common_common)
                and isinstance(second, ast.Constant)
                and isinstance(second.value, str)
                and not _is_allowed(second.value)
            ):
                report(node, f"names common.common.{second.value} in a call")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            match = _DOTTED_TARGET.fullmatch(node.value)
            if match and not _is_allowed(match["name"]):
                report(node, f"string target {node.value!r}")

    return [message for _line, message in sorted(found)]


def _bound_names(target: ast.AST) -> set[str]:
    """Return the names that an assignment, ``for`` or ``with`` target binds."""
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return set().union(*(_bound_names(item) for item in target.elts))
    if isinstance(target, ast.Starred):
        return _bound_names(target.value)
    return set()


def module_level_names(source: str) -> set[str]:
    """Return the names that the module binds at its top level, without dunder names.

    The walk enters ``if``, ``try``, ``with``, ``for`` and ``while`` blocks. It does not enter
    a function or a class body, because their names are not module attributes.
    """
    names: set[str] = set()

    def visit(statements: list[ast.stmt]) -> None:
        for node in statements:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Import):
                names.update(
                    item.asname or item.name.split(".")[0] for item in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                names.update(item.asname or item.name for item in node.names)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    names.update(_bound_names(target))
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                names.update(_bound_names(node.target))
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                names.update(_bound_names(node.target))
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        names.update(_bound_names(item.optional_vars))
            for field in ("body", "orelse", "finalbody"):
                inner = getattr(node, field, None)
                if inner and not isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    visit(inner)
            if isinstance(node, ast.Try):
                for handler in node.handlers:
                    if handler.name:
                        names.add(handler.name)
                    visit(handler.body)

    visit(ast.parse(source).body)
    return {
        name for name in names if not (name.startswith("__") and name.endswith("__"))
    }


def module_name_drift(source: str) -> list[str]:
    """Return one line per difference between the module-level names and the allowlist."""
    names = module_level_names(source)
    return [f"binds {name}" for name in sorted(names - ALLOWED_MODULE_NAMES)] + [
        f"no longer binds {name}" for name in sorted(ALLOWED_MODULE_NAMES - names)
    ]


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
        "Import environment helpers from common.core.environ. "
        "common.common offers check_service only (#1164):\n" + "\n".join(found)
    )


def test_libs_common_binds_only_the_allowed_module_names() -> None:
    """The AST of common/common.py must give exactly ``ALLOWED_MODULE_NAMES``."""
    source = (ROOT / "common" / "common.py").read_text(encoding="utf-8")
    assert module_name_drift(source) == []


def test_libs_common_offers_check_service_only() -> None:
    import common.common as common

    assert common.__all__ == ["check_service"]
    assert callable(common.check_service)


# --- Self-tests: the guard must fail on a violation and pass on compliant code. ---------

_COMPLIANT_COMMON = (
    "from __future__ import annotations\n"
    "import shlex\n"
    "from typing import TYPE_CHECKING\n"
    "from common.core import environ\n"
    "if TYPE_CHECKING:\n"
    "    from invoke import Context\n"
    "__all__ = ['check_service']\n"
    "def check_service(c, service, health_cmd):\n"
    "    env = environ.get_env()\n"
    "    container = shlex.quote(service)\n"
    "    return env, container\n"
)


def test_name_check_passes_a_module_with_the_allowed_names_only() -> None:
    assert module_name_drift(_COMPLIANT_COMMON) == []


def test_name_check_flags_a_private_name_that_environ_does_not_export() -> None:
    # environ.__all__ omits this name, so a check on that list cannot see it.
    imported = _COMPLIANT_COMMON + (
        "from common.core.environ import _REGISTRY_BACKED_SHORT_NAMES\n"
    )
    assert module_name_drift(imported) == ["binds _REGISTRY_BACKED_SHORT_NAMES"]
    assigned = _COMPLIANT_COMMON + "_BOOTSTRAP_ONLY_SHARED_SERVICES = frozenset()\n"
    assert module_name_drift(assigned) == ["binds _BOOTSTRAP_ONLY_SHARED_SERVICES"]


def test_name_check_flags_a_name_that_leaves_the_allowlist() -> None:
    source = _COMPLIANT_COMMON.replace("import shlex\n", "")
    assert module_name_drift(source) == ["no longer binds shlex"]


def test_name_check_reads_every_binding_form_at_module_level() -> None:
    source = (
        "import os.path\n"
        "import numpy as np\n"
        "from a import b as c, d\n"
        "from a import *\n"
        "x, (y, *z) = 1, (2, 3)\n"
        "w: int = 1\n"
        "n += 1\n"
        "for i in []:\n"
        "    pass\n"
        "with open('f') as fh:\n"
        "    pass\n"
        "try:\n"
        "    import q\n"
        "except ImportError as err:\n"
        "    r = 1\n"
        "else:\n"
        "    s = 1\n"
        "finally:\n"
        "    t = 1\n"
        "if x:\n"
        "    def f(): inner = 1\n"
        "    class K:\n"
        "        attr = 1\n"
        "async def g(): pass\n"
        "__all__ = []\n"
    )
    assert module_level_names(source) == {
        "os", "np", "c", "d", "*", "x", "y", "z", "w", "n", "i", "fh", "q", "err",
        "r", "s", "t", "f", "K", "g",
    }  # fmt: skip


def test_guard_flags_a_from_import_of_an_environment_helper() -> None:
    source = "from common.common import check_service, get_env\n"
    assert libs_common_violations(source) == [
        "<memory>.py:1: imports get_env from common.common"
    ]


def test_guard_flags_a_multi_line_import_and_an_import_inside_a_function() -> None:
    source = (
        "from common.common import (\n"
        "    check_service,\n"
        "    SERVICE_SUBDOMAINS as subdomains,\n"
        ")\n"
        "\n"
        "def f():\n"
        "    from common.common import with_env_suffix\n"
    )
    assert libs_common_violations(source) == [
        "<memory>.py:1: imports SERVICE_SUBDOMAINS from common.common",
        "<memory>.py:7: imports with_env_suffix from common.common",
    ]


def test_guard_flags_a_star_import() -> None:
    assert libs_common_violations("from common.common import *\n") == [
        "<memory>.py:1: imports * from common.common"
    ]


def test_guard_flags_attribute_reads_through_every_alias_form() -> None:
    source = (
        "import common.common as c\n"
        "from common import common\n"
        "from common import common as m\n"
        "import common.common\n"
        "c.get_env()\n"
        "common.infra_domain()\n"
        "m.SERVICE_SUBDOMAINS\n"
        "common.common.validate_env()\n"
        "c.check_service\n"
        "c.__all__\n"
    )
    assert libs_common_violations(source) == [
        "<memory>.py:5: reads common.common.get_env",
        "<memory>.py:6: reads common.common.infra_domain",
        "<memory>.py:7: reads common.common.SERVICE_SUBDOMAINS",
        "<memory>.py:8: reads common.common.validate_env",
    ]


def test_guard_flags_attribute_names_given_as_strings() -> None:
    source = (
        "from common import common\n"
        "hasattr(common, 'DeploymentEnvironment')\n"
        "monkeypatch.setattr(common, 'get_env', f)\n"
        "patch.object(common, 'check_service')\n"
    )
    assert libs_common_violations(source) == [
        "<memory>.py:2: names common.common.DeploymentEnvironment in a call",
        "<memory>.py:3: names common.common.get_env in a call",
    ]


def test_guard_flags_a_string_patch_target() -> None:
    source = (
        "monkeypatch.setattr('common.common.get_env', f)\n"
        "mock.patch('common.common.check_service')\n"
        "'common.common.not_a_target is prose, not a target'\n"
    )
    assert libs_common_violations(source) == [
        "<memory>.py:1: string target 'common.common.get_env'"
    ]


def test_guard_flags_relative_imports_that_reach_libs_common() -> None:
    source = "from . import common\nfrom .common import get_env\ncommon.get_env\n"
    assert libs_common_violations(source, "common/x.py") == [
        "common/x.py:2: imports get_env from common.common",
        "common/x.py:3: reads common.common.get_env",
    ]


def test_guard_resolves_parent_relative_imports_from_a_subpackage() -> None:
    source = (
        "from .. import common as up\n"
        "from ..common import get_env\n"
        "from . import common\n"
        "up.service_domain\n"
        "common.get_env\n"
    )
    # Line 3 names common.core.common, a different module, so line 5 is not a violation.
    assert libs_common_violations(source, "common/core/x.py") == [
        "common/core/x.py:2: imports get_env from common.common",
        "common/core/x.py:4: reads common.common.service_domain",
    ]


def test_guard_passes_compliant_code() -> None:
    source = (
        "from common.common import check_service\n"
        "from common.core.environ import get_env, infra_domain\n"
        "from common import common\n"
        "import common.common as c\n"
        "\n"
        "def f(ctx):\n"
        "    from common.common import check_service as health\n"
        "    common.check_service(ctx, 'redis', 'true')\n"
        "    c.check_service\n"
        "    patch('common.common.check_service')\n"
        "    return get_env(), infra_domain(), health\n"
    )
    assert libs_common_violations(source) == []


def test_guard_ignores_a_relative_import_of_another_module() -> None:
    source = "from .environ import get_env\nfrom . import environ\n"
    assert libs_common_violations(source, "common/core/x.py") == []
