"""Shared test setup for common/tests."""

from __future__ import annotations

import subprocess

import pytest

# The function a ``OnePasswordBackend`` built without a ``runner`` holds. Read before any test
# replaces ``subprocess.run``.
_REAL_SUBPROCESS_RUN = subprocess.run

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


@pytest.fixture(autouse=True)
def _no_ambient_op_cli(monkeypatch):
    """Keep unit tests off the 1Password CLI of the developer shell.

    CI has no ``op`` binary, so ``OpSecrets`` reads an empty item there. A shell with ``op``
    made the same tests read the real vault and wait up to 30 s each (#1453). A backend
    with its own ``runner`` is unchanged."""
    from infra2_sdk.secrets import OnePasswordBackend

    real_op = OnePasswordBackend._op

    def guarded_op(self, *args, **kwargs):
        if self._run is _REAL_SUBPROCESS_RUN:
            raise FileNotFoundError(2, "the op CLI is not available in unit tests")
        return real_op(self, *args, **kwargs)

    monkeypatch.setattr(OnePasswordBackend, "_op", guarded_op)
