"""The iac-runner imports only infra2-sdk names that its own SDK has (#1610).

The runner image installs infra2-sdk from uv.lock when it is built. A rebuild is
a production change, so the SDK in the image can be older than the pin of main.
Run 43012402667 (v3.1.50) failed: the image held 3.23.0 and `dokploy_client`
imported `request_with_retry` from 3.27.0. `invoke <service>.sync` loads the
whole task collection, so one bad import failed every service.

This test loads that collection the way the runner does, follows each repo
import of the loaded modules (function-level imports included, #1853), and lists
the infra2_sdk names that those files import. Each name must be in
RUNNER_SDK_NAMES. An entry needs a check against the runner wheel
(`pip install infra2-sdk==<RUNNER_SDK>`); the production run that rebuilds the
runner raises RUNNER_SDK and may add names.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER_SDK = "3.23.0"
RUNNER_SDK_NAMES = frozenset(
    {
        ("infra2_sdk.ci", "parse_github_time"),
        ("infra2_sdk.deploy", "canonical_json_bytes"),
        ("infra2_sdk.observability", "CheckVerdict"),
        ("infra2_sdk.observability", "ConsecutiveObservationState"),
        ("infra2_sdk.observability", "GitHubIssues"),
        ("infra2_sdk.observability", "IssueApi"),
        ("infra2_sdk.observability", "Trail"),
        ("infra2_sdk.observability", "evaluate_consecutive_hysteresis"),
        ("infra2_sdk.observability", "is_recently_flapping"),
        ("infra2_sdk.observability", "load_trail"),
        ("infra2_sdk.observability", "reconcile_issue_trail"),
        ("infra2_sdk.observability", "record_verdicts"),
        ("infra2_sdk.refs", "ReleaseError"),
        ("infra2_sdk.refs", "assert_after_on_main"),
        ("infra2_sdk.refs", "resolve_image_digest"),
        ("infra2_sdk.routing", "CANARY_SLOT"),
        ("infra2_sdk.routing", "derive_preview_coordinates"),
        ("infra2_sdk.routing", "resolve_app_hostname"),
        ("infra2_sdk.routing", "resolve_service_url"),
        ("infra2_sdk.runtime.config_schema", "EnvironmentField"),
        ("infra2_sdk.runtime.config_schema", "EnvironmentManifest"),
        ("infra2_sdk.runtime.environment", "is_preview_alias"),
        ("infra2_sdk.runtime.environment", "resolve_environment_tier"),
        ("infra2_sdk.runtime.health", "DependencyStatus"),
        ("infra2_sdk.runtime.otel", "signal_endpoint"),
        ("infra2_sdk.runtime.postgres", "PostgresSettings"),
        ("infra2_sdk.runtime.postgres", "probe_postgres"),
        ("infra2_sdk.runtime.s3", "S3Settings"),
        ("infra2_sdk.runtime.s3", "ensure_bucket"),
        ("infra2_sdk.runtime.s3", "probe_s3"),
        ("infra2_sdk.secrets", "HttpTransport"),
        ("infra2_sdk.secrets", "OnePasswordBackend"),
        ("infra2_sdk.secrets", "SecretsError"),
        ("infra2_sdk.secrets", "SecretsResolver"),
        ("infra2_sdk.secrets", "VaultKvBackend"),
        ("infra2_sdk.secrets", "VaultTokenTarget"),
        ("infra2_sdk.secrets", "normalize_selector"),
        ("infra2_sdk.secrets", "policy_name"),
        ("infra2_sdk.secrets", "urllib_transport"),
        ("infra2_sdk.secrets", "vault_path"),
        ("infra2_sdk.secrets", "vault_token_status"),
    }
)

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
        if isinstance(node, ast.Dict):
            # PEP 562 lazy maps: {"Name": "infra2_sdk.module"} loads Name on first use.
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and value.value.split(".")[0] == "infra2_sdk"
                ):
                    names.add((value.value, key.value))
        elif isinstance(node, ast.ImportFrom) and node.module:
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
            # A string module path: PEP 562 lazy maps and importlib.import_module.
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


def test_the_scan_follows_a_lazy_map(tmp_path: Path) -> None:
    """A PEP 562 map loads modules by string (#1853 review follow-up, infra2#1823)."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        '_LAZY = {"run_probes": "common.observability.probes",'
        ' "Trail": "infra2_sdk.observability"}\n',
        encoding="utf-8",
    )
    probes = (ROOT / "common/observability/probes.py").resolve()
    assert probes in local_files_imported_by(probe)
    assert ("infra2_sdk.observability", "Trail") in sdk_names_imported_by(
        probe.read_text(encoding="utf-8")
    )


def test_import_scanner_reports_the_name_that_broke_the_runner() -> None:
    source = "from infra2_sdk.runtime.http import request_with_retry\nimport infra2_sdk.secrets\n"
    assert sdk_names_imported_by(source) == {
        ("infra2_sdk.runtime.http", "request_with_retry"),
        ("infra2_sdk.secrets", ""),
    }
    assert ("infra2_sdk.runtime.http", "request_with_retry") not in RUNNER_SDK_NAMES


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

    unknown = {
        pair: file for pair, file in imported.items() if pair not in RUNNER_SDK_NAMES
    }
    assert not unknown, (
        f"These infra2_sdk names are not known to exist in the runner SDK {RUNNER_SDK}: "
        + "; ".join(f"{m}.{n} ({f})" for (m, n), f in sorted(unknown.items()))
        + ". A runner rebuild is a production change. Check the names against the runner wheel first."
    )
