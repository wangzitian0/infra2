"""Shared test setup for common/tests."""

from __future__ import annotations

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


@pytest.fixture(scope="session", autouse=True)
def _parse_each_yaml_text_once():
    """Parse each YAML text once per worker (#1453). See ``common/tests/yaml_cache.py``."""
    import yaml

    from common.tests.yaml_cache import ParseOnce

    real_safe_load = yaml.safe_load
    yaml.safe_load = ParseOnce(real_safe_load)
    yield
    yaml.safe_load = real_safe_load
