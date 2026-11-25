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
def _no_github_retry_wait(monkeypatch):
    """A fake GitHub 5xx is sent again without the real backoff wait of ``github_api``."""
    from common.deploy import github_api

    monkeypatch.setattr(github_api, "_sleep", lambda _seconds: None)


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


@pytest.fixture
def fake_iac_ref_reads_the_working_tree(monkeypatch):
    """promote reads the stack file at iac_ref (#1822 Slice 3, deploy provenance). The
    fake ref ``"b" * 40`` reads the working tree; every other ref reads git."""
    import common.deploy.config_drift as config_drift

    real = config_drift.contents_at_ref
    root = Path(__file__).resolve().parents[2]

    def contents_at_ref(ref, paths):
        if ref != "b" * 40:
            return real(ref, paths)
        return {p: (root / p).read_bytes() for p in paths if (root / p).is_file()}

    monkeypatch.setattr(config_drift, "contents_at_ref", contents_at_ref)


@pytest.fixture(autouse=True)
def _no_live_runner_sdk_check(request, monkeypatch):
    """A reconcile test does not read the live IaC runner ``/health`` (#1822).

    ``common/tests/test_reconcile_runner_sdk.py`` tests the real check; a test whose
    name contains ``runner_sdk`` keeps it."""
    if "runner_sdk" in request.node.name or "runner_sdk" in request.path.name:
        return
    import common.deploy.reconcile_iac_inputs as reconcile

    monkeypatch.setattr(
        reconcile,
        "assert_runner_sdk_covers_ref",
        lambda *_a, **_k: "runner SDK check stubbed in unit tests",
    )
