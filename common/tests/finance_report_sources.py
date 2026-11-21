"""Read the pinned finance_report sources that the alert and dashboard contracts check (#1816).

The pin is the `repos/finance_report` gitlink of this checkout. A local checkout at the
pinned commit supplies the files. Infra CI checks out only PINNED_FILES, outside the infra2
tree, in the step "Check out the pinned finance_report contract files" of infra-ci.yml (#506).
That step exports the path as FINANCE_REPORT_PINNED_CHECKOUT. With `CI` set, the reader uses
only that path. A local run uses repos/finance_report when it is at the pin, and otherwise
fetches the files at the pinned commit from the public repository.

Every read is lazy: a test calls the reader, an import never does. An unreadable source fails
only the tests that read it, with one PinnedSourceUnavailable message, and the reader keeps
that message, so the failure costs one fetch attempt.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path

from common.core.constants import APP_SOURCES, SERVICE_TO_SUBMODULE_PATH
from common.core.fetch_app_manifests import http_get

ROOT = Path(__file__).resolve().parents[2]
SERVICE_ID = "finance_report/app"
TELEMETRY_METRICS = "apps/backend/src/observability/telemetry_metrics.py"
OPENAPI = "apps/frontend/openapi.json"
CLASSIFICATION_RUN = (
    "apps/backend/src/extraction/extension/statement_classification_run.py"
)
# The infra-ci.yml step checks out these files and no others.
PINNED_FILES = (TELEMETRY_METRICS, OPENAPI, CLASSIFICATION_RUN)
# The infra-ci.yml step writes the path of its checkout to this variable.
PINNED_CHECKOUT_ENV = "FINANCE_REPORT_PINNED_CHECKOUT"
CI_STEP = "Check out the pinned finance_report contract files"

# The OTel FastAPI HTTP server metrics, as SigNoz stores them (#1465).
OTEL_HTTP_SERVER_METRICS = frozenset(
    {"http.server.request.duration.bucket", "http.server.request.duration.count"}
)
# SigNoz stores one OTel histogram as these three series.
_HISTOGRAM_SERIES = (".bucket", ".count", ".sum")
# A counter whose name ends in one of these words counts a failure.
_FAILURE_COUNTER = re.compile(r"(?:^|[._])(failure|violation|dead_letter|rejected)$")


class PinnedSourceUnavailable(RuntimeError):
    """A pinned finance_report source cannot be read."""


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip()


def pinned_sha(root: Path = ROOT) -> str:
    """Return the commit of the finance_report gitlink in the index of ``root``."""
    path = SERVICE_TO_SUBMODULE_PATH[SERVICE_ID]
    mode, sha, _stage, _name = _git("ls-files", "--stage", "--", path, cwd=root).split()
    if mode != "160000":
        raise PinnedSourceUnavailable(f"{path} is not a gitlink (mode {mode})")
    return sha


def _at_pin(checkout: Path, sha: str) -> bool:
    return (checkout / ".git").exists() and _git(
        "rev-parse", "HEAD", cwd=checkout
    ) == sha


def _read(relative_path: str) -> str:
    sha = pinned_sha()
    if os.environ.get("CI"):
        configured = os.environ.get(PINNED_CHECKOUT_ENV, "")
        if not configured or not _at_pin(Path(configured), sha):
            raise PinnedSourceUnavailable(
                f"CI reads only the checkout in {PINNED_CHECKOUT_ENV} "
                f"({configured or 'not set'}), and it is missing or not at {sha}. "
                f'Run the infra-ci.yml step "{CI_STEP}" before pytest. '
                "PINNED_FILES must list each file that a test reads."
            )
        return (Path(configured) / relative_path).read_text(encoding="utf-8")
    checkout = ROOT / SERVICE_TO_SUBMODULE_PATH[SERVICE_ID]
    if _at_pin(checkout, sha):
        return (checkout / relative_path).read_text(encoding="utf-8")
    url = f"https://raw.githubusercontent.com/{APP_SOURCES[SERVICE_ID]}/{sha}/{relative_path}"
    try:
        return http_get(url).decode("utf-8")
    except SystemExit as exc:  # http_get exits on a permanent error
        raise PinnedSourceUnavailable(str(exc)) from exc


_READS: dict[str, str | PinnedSourceUnavailable] = {}


def read_pinned(relative_path: str) -> str:
    """Return the text of ``relative_path`` in finance_report at the pinned commit."""
    if relative_path not in _READS:
        try:
            _READS[relative_path] = _read(relative_path)
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
            _READS[relative_path] = PinnedSourceUnavailable(
                f"cannot read finance_report {relative_path} at the pinned commit: {exc}"
            )
    result = _READS[relative_path]
    if isinstance(result, PinnedSourceUnavailable):
        raise PinnedSourceUnavailable(str(result))
    return result


def _module_tuple(tree: ast.Module, name: str) -> tuple:
    for node in tree.body:
        targets = (
            [node.target]
            if isinstance(node, ast.AnnAssign)
            else node.targets
            if isinstance(node, ast.Assign)
            else []
        )
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"telemetry_metrics.py has no module-level {name}")


def _gauge_names(tree: ast.Module) -> set[str]:
    """Names in the `for metric_name, cb, desc in (...)` loop of `_create_instruments`."""
    (function,) = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_create_instruments"
    ]
    names = {
        spec.elts[0].value
        for loop in ast.walk(function)
        if isinstance(loop, ast.For) and isinstance(loop.iter, ast.Tuple)
        for spec in loop.iter.elts
        if isinstance(spec, ast.Tuple)
        and isinstance(spec.elts[0], ast.Constant)
        and isinstance(spec.elts[0].value, str)
    }
    assert names, "_create_instruments registers no observable gauge"
    return names


def counter_names(source: str) -> set[str]:
    """Metric names of `_COUNTER_SPECS` in telemetry_metrics.py."""
    return {spec[1] for spec in _module_tuple(ast.parse(source), "_COUNTER_SPECS")}


def failure_counter_names(source: str) -> set[str]:
    """Counters that count a failure, a violation, a dead letter or a rejection."""
    return {name for name in counter_names(source) if _FAILURE_COUNTER.search(name)}


def exported_metric_names(source: str) -> set[str]:
    """Every series name that telemetry_metrics.py exports to SigNoz."""
    tree = ast.parse(source)
    histograms = {spec[1] for spec in _module_tuple(tree, "_HISTOGRAM_SPECS")}
    return (
        counter_names(source)
        | {name + suffix for name in histograms for suffix in _HISTOGRAM_SERIES}
        | _gauge_names(tree)
    )


def service_metrics(source: str) -> dict[str, set[str]]:
    """The metrics each finance-report service exports. Only the backend serves HTTP."""
    app = exported_metric_names(source)
    return {
        "finance-report-backend": app | OTEL_HTTP_SERVER_METRICS,
        "finance-report-worker": app,
        "finance-report-frontend": set(),
    }


def openapi_routes(text: str) -> set[str]:
    """Route templates of openapi.json. FastAPI writes the same template to `http.route`."""
    return set(json.loads(text)["paths"])


def classify_outcomes(source: str) -> set[str]:
    """Values of the `ClassifyOutcome` enum: the `outcome` label of the classification counter."""
    (enum,) = [
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef) and node.name == "ClassifyOutcome"
    ]
    return {
        stmt.value.value
        for stmt in enum.body
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant)
    }


@lru_cache(maxsize=None)
def pinned_service_metrics() -> dict[str, set[str]]:
    """`service_metrics` of the pinned telemetry_metrics.py, read on the first call."""
    return service_metrics(read_pinned(TELEMETRY_METRICS))


@lru_cache(maxsize=None)
def pinned_classify_outcomes() -> set[str]:
    """`classify_outcomes` of the pinned classification module, read on the first call."""
    return classify_outcomes(read_pinned(CLASSIFICATION_RUN))


@lru_cache(maxsize=None)
def pinned_openapi_routes() -> set[str]:
    """`openapi_routes` of the pinned openapi.json, read on the first call."""
    return openapi_routes(read_pinned(OPENAPI))
