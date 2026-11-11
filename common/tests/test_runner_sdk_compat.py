"""The iac-runner imports only infra2-sdk names that its own SDK has (#1610).

The runner image installs infra2-sdk from uv.lock when it is built. A rebuild is
a production change, so the SDK in the image can be older than the pin of main.
Run 43012402667 (v3.1.50) failed: the image held 3.23.0 and `dokploy_client`
imported `request_with_retry` from 3.27.0. `invoke <service>.sync` loads the
whole task collection, so one bad import failed every service.

This test loads that collection the way the runner does and lists the
infra2_sdk names that the loaded modules import. Each name must be in
RUNNER_SDK_NAMES. An entry needs a check against the runner wheel
(`pip install infra2-sdk==<RUNNER_SDK>`); the production run that rebuilds the
runner raises RUNNER_SDK and may add names.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER_SDK = "3.23.0"
RUNNER_SDK_NAMES = frozenset(
    {
        ("infra2_sdk.ci", "parse_github_time"),
        ("infra2_sdk.observability", "is_recently_flapping"),
        ("infra2_sdk.refs", "resolve_image_digest"),
        ("infra2_sdk.routing", "CANARY_SLOT"),
        ("infra2_sdk.routing", "derive_preview_coordinates"),
        ("infra2_sdk.routing", "resolve_app_hostname"),
        ("infra2_sdk.routing", "resolve_service_url"),
        ("infra2_sdk.runtime.environment", "is_preview_alias"),
        ("infra2_sdk.runtime.environment", "resolve_environment_tier"),
        ("infra2_sdk.runtime.otel", "signal_endpoint"),
        ("infra2_sdk.runtime.s3", "S3Settings"),
        ("infra2_sdk.runtime.s3", "ensure_bucket"),
        ("infra2_sdk.secrets", "OnePasswordBackend"),
        ("infra2_sdk.secrets", "SecretsError"),
        ("infra2_sdk.secrets", "VaultKvBackend"),
        ("infra2_sdk.secrets", "VaultTokenTarget"),
        ("infra2_sdk.secrets", "normalize_selector"),
        ("infra2_sdk.secrets", "policy_name"),
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
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] == "infra2_sdk":
                names |= {(node.module, alias.name) for alias in node.names}
        elif isinstance(node, ast.Import):
            names |= {
                (a.name, "") for a in node.names if a.name.split(".")[0] == "infra2_sdk"
            }
    return names


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

    imported: dict[tuple[str, str], str] = {}
    for file in loaded:
        for pair in sdk_names_imported_by(Path(file).read_text(encoding="utf-8")):
            imported.setdefault(pair, Path(file).relative_to(ROOT).as_posix())
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
