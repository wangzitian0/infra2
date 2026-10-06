"""Shared test setup for libs/tests."""

from __future__ import annotations

import sys

import pytest

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
    from libs.core import environ

    for name in _ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    environ.reset_env_cache()
    yield
    environ.reset_env_cache()


def pytest_configure(config):
    """Fill the app-manifest cache once, before any test worker starts (#1085).

    CI runs ``tools/fetch_app_manifests.py`` before pytest. A worktree without the
    app submodules otherwise depended on whichever test fetched a manifest first:
    ``test_every_registered_manifest_is_resolvable`` only checks that the file
    exists, and failed whenever its xdist worker ran before the fetching test.
    Offline, the fetch fails here with a warning and the manifest tests fail on
    their own assertions.
    """
    if hasattr(config, "workerinput"):
        return  # an xdist worker: the controller fetched before starting it
    from tools import fetch_app_manifests

    try:
        fetch_app_manifests.main()
    except Exception as exc:  # noqa: BLE001 - report; the dependent tests still fail
        print(f"conftest: app manifest prefetch failed: {exc}", file=sys.stderr)
