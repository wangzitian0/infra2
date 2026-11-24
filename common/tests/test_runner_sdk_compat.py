"""The iac-runner imports only infra2-sdk names that its own SDK has (#1610, #1863).

The runner image installs infra2-sdk from uv.lock when it is built. Since #1863 a change
of uv.lock or of tools/sdk_requirement*.py rebuilds the image, so the SDK of the runner is the
SDK that uv.lock pins. Run 43012402667 (v3.1.50) failed when the image held 3.23.0 and
`dokploy_client` imported `request_with_retry` from 3.27.0: `invoke <service>.sync` loads the
whole task collection, so one bad import failed every service.

This test loads that collection the way the runner does, follows each repo import of the
loaded modules (function-level imports included, #1853), and lists the infra2_sdk names that
those files import. Each name must exist in the installed wheel, and the installed wheel must
be the one uv.lock pins. No list of names is kept here: a list pinned old names (4.0.0) while
uv.lock moved to 4.4.0.
"""

from __future__ import annotations

import ast
import importlib
import importlib.metadata
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def locked_sdk_version() -> str:
    """The infra2-sdk version that uv.lock pins, and so the version of the rebuilt runner."""
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    versions = [p["version"] for p in lock["package"] if p["name"] == "infra2-sdk"]
    assert len(versions) == 1, versions
    return versions[0]


def names_missing_from_installed_sdk(
    names: set[tuple[str, str]],
) -> list[tuple[str, str]]:
    """The (module, name) pairs that the installed infra2_sdk wheel does not provide."""
    missing = []
    for module, name in sorted(names):
        try:
            loaded = importlib.import_module(module)
            if name and not hasattr(loaded, name):
                importlib.import_module(f"{module}.{name}")
        except ImportError:
            missing.append((module, name))
    return missing


# Same module list as the runner: `INVOKE_BOOTSTRAP` in sync_runner.py.
LOAD_COLLECTION = (
    "import json, os, runpy, sys\n"
    "sys.path.insert(0, '.')\n"
    "sys.argv = ['invoke', '--list']\n"
    "try:\n"
    "    runpy.run_module('invoke', run_name='__main__')\n"
    "except SystemExit:\n"
    "    pass\n"
    "root = os.getcwd() + os.sep\n"
    "files = {m.__file__ for m in sys.modules.values()\n"
    "         if getattr(m, '__file__', None) and m.__file__.startswith(root)\n"
    "         and '.venv' not in m.__file__}\n"
    "print('LOADED=' + json.dumps(sorted(files)))\n"
)


def sdk_names_imported_by(source: str) -> set[tuple[str, str]]:
    names: set[tuple[str, str]] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] == "infra2_sdk":
                names |= {(node.module, alias.name) for alias in node.names}
        elif isinstance(node, ast.Import):
            names |= {
                (a.name, "") for a in node.names if a.name.split(".")[0] == "infra2_sdk"
            }
    return names


_LOCAL_ROOTS = frozenset(
    {"common", "tools", "platform", "bootstrap", "libs", "finance_report", "truealpha"}
)


_MODULE_PATH_RE = re.compile(r"[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)+")


def local_files_imported_by(path: Path) -> set[Path]:
    """Repo files that ``path`` imports, function-level imports included.

    The runner calls functions that import modules ``invoke --list`` never loads (the
    secret supply path imports ``common.security.registry``). #1853 review: such a module
    imported an SDK 4.x name that the runner SDK lacks, and the old scan missed it."""
    found: set[Path] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            modules = [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
        elif isinstance(node, ast.Import):
            modules = [a.name for a in node.names]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # A string module path, as importlib.import_module takes it.
            if not _MODULE_PATH_RE.fullmatch(node.value):
                continue
            modules = [node.value]
        else:
            continue
        for module in modules:
            if module.split(".")[0] not in _LOCAL_ROOTS:
                continue
            base = ROOT.joinpath(*module.split("."))
            for candidate in (base.with_suffix(".py"), base / "__init__.py"):
                if candidate.is_file():
                    found.add(candidate.resolve())
    return found


def runner_reachable_files(loaded: list[str]) -> set[Path]:
    """The loaded modules plus every repo file that they import, transitively."""
    seen = {Path(f).resolve() for f in loaded}
    queue = list(seen)
    while queue:
        for target in local_files_imported_by(queue.pop()):
            if target not in seen:
                seen.add(target)
                queue.append(target)
    return seen


def test_the_reach_scan_follows_a_function_level_import(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(
        "def supply():\n    from common.security.registry import merged_manifest\n",
        encoding="utf-8",
    )
    registry = (ROOT / "common/security/registry.py").resolve()
    assert registry in local_files_imported_by(probe)


def test_the_scan_follows_a_string_module_path(tmp_path: Path) -> None:
    """`importlib.import_module` loads a module by string (#1853 review follow-up)."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        'import importlib\nimportlib.import_module("common.observability.probes")\n',
        encoding="utf-8",
    )
    probes = (ROOT / "common/observability/probes.py").resolve()
    assert probes in local_files_imported_by(probe)


def test_import_scanner_reports_the_name_that_broke_the_runner() -> None:
    source = "from infra2_sdk.runtime.http import request_with_retry\nimport infra2_sdk.secrets\n"
    assert sdk_names_imported_by(source) == {
        ("infra2_sdk.runtime.http", "request_with_retry"),
        ("infra2_sdk.secrets", ""),
    }
    assert names_missing_from_installed_sdk(
        {("infra2_sdk.runtime.http", "request_with_retry_gone")}
    ) == [("infra2_sdk.runtime.http", "request_with_retry_gone")]
    assert (
        names_missing_from_installed_sdk({("infra2_sdk.secrets", "SecretsError")}) == []
    )


def test_the_installed_sdk_is_the_wheel_that_uv_lock_pins() -> None:
    assert importlib.metadata.version("infra2-sdk") == locked_sdk_version()


def test_runner_task_collection_imports_only_names_of_the_runner_sdk() -> None:
    result = subprocess.run(
        [sys.executable, "-c", LOAD_COLLECTION],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    line = next(
        (x for x in result.stdout.splitlines() if x.startswith("LOADED=")), None
    )
    assert line is not None, (
        f"collection did not load (exit {result.returncode}):\n{result.stderr[-1500:]}"
    )
    loaded = json.loads(line.removeprefix("LOADED="))
    assert len(loaded) > 20, (
        f"only {len(loaded)} repo modules loaded; the scan would be empty"
    )

    reachable = runner_reachable_files(loaded)
    assert (ROOT / "common/security/registry.py").resolve() in reachable, (
        "the secret supply module is not in the scan; the reach scan is broken"
    )
    imported: dict[tuple[str, str], str] = {}
    for file in sorted(reachable):
        for pair in sdk_names_imported_by(file.read_text(encoding="utf-8")):
            imported.setdefault(pair, file.relative_to(ROOT).as_posix())
    assert imported, (
        "no infra2_sdk import found in the loaded modules; the scan is empty"
    )

    missing = names_missing_from_installed_sdk(set(imported))
    assert not missing, (
        f"These infra2_sdk names are not in the SDK {locked_sdk_version()} that uv.lock pins: "
        + "; ".join(f"{m}.{n} ({imported[(m, n)]})" for m, n in missing)
        + ". The runner image installs that SDK, so every task collection load would fail."
    )
