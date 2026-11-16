"""Shared test setup for common/tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def load_repo_module(rel_path: str | Path, module_name: str):
    """Load any repo module (including digit-prefixed platform dirs) cleanly."""
    target = Path(rel_path)
    path = target if target.is_absolute() else _REPO_ROOT / target
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader, f"Cannot load module {module_name!r} from {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def evaluate_keyed_ratchet(
    actual: dict[str, list[str]],
    baseline: dict[str, list[str]],
    keys: tuple[str, ...],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Return (new_regressions, stale_baseline_entries) across ratchet keys."""
    regressions: dict[str, list[str]] = {}
    stale: dict[str, list[str]] = {}
    for key in keys:
        actual_set = set(actual.get(key, []))
        base_set = set(baseline.get(key, []))
        added = sorted(actual_set - base_set)
        removed = sorted(base_set - actual_set)
        if added:
            regressions[key] = added
        if removed:
            stale[key] = removed
    return regressions, stale


def assert_backend_cold_start_budget(compose_path: Path, end_marker: str) -> None:
    """Assert backend healthcheck start_period covers cold-start migrations."""
    import re

    source = compose_path.read_text(encoding="utf-8")
    backend_start = source.index("\n  backend:")
    backend_end = source.index(end_marker)
    backend_block = source[backend_start:backend_end]
    match = re.search(r"start_period:\s*([0-9]+)s", backend_block)
    assert match is not None
    assert int(match.group(1)) >= 450


# The variables that choose the deployment environment of a process. ``get_env`` has no
# default (#1039), so a test that needs an environment sets it, and no test inherits one
# from the developer shell or from an earlier test in the same worker.
_ENVIRONMENT_VARIABLES = (
    "INFRA_ENVIRONMENT",
    "DEPLOY_ENV",
    "ENV_SUFFIX",
    "ENV_DOMAIN_SUFFIX",
)


@pytest.fixture(autouse=True)
def _no_ambient_deployment_environment(monkeypatch):
    from common.core import environ

    for name in _ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    environ.reset_env_cache()
    yield
    environ.reset_env_cache()


def _op_cli_is_absent(argv, *_args, **_kwargs):
    """A runner that behaves as a machine without the ``op`` binary."""
    raise FileNotFoundError(2, "the op CLI is not available in unit tests", argv[0])


@pytest.fixture(autouse=True)
def _stage_records_off_the_worktree(monkeypatch, tmp_path):
    """A test that runs a reconcile writes its stage records into its own directory (#1645)."""
    monkeypatch.setenv("STAGE_RECORD_DIR", str(tmp_path / "stage-records"))


@pytest.fixture(autouse=True)
def _no_ambient_op_cli(monkeypatch):
    """Keep unit tests off the 1Password CLI of the developer shell.

    CI has no ``op`` binary, so ``OpSecrets`` reads an empty item there. A shell with ``op``
    made the same tests read the real vault and wait up to 30 s each (#1453). A backend
    built with its own ``runner`` is unchanged."""
    from infra2_sdk.secrets import OnePasswordBackend

    real_init = OnePasswordBackend.__init__

    def init(self, *args, runner=None, **kwargs):
        runner = _op_cli_is_absent if runner is None else runner
        real_init(self, *args, runner=runner, **kwargs)

    monkeypatch.setattr(OnePasswordBackend, "__init__", init)
