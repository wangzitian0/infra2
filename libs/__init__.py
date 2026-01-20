# Infra2 shared libraries
#
# NOTE: Imports are lazy to avoid circular dependencies.
# Import specific modules directly when needed:
#   from libs.env import get_secrets, OpSecrets, VaultSecrets
#   from libs.common import get_env
#   from libs.console import header, success
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from libs.env import get_secrets, OpSecrets, VaultSecrets, generate_password
    from libs.common import get_env, validate_env, check_service, CONTAINERS
    from libs.console import (
        header,
        success,
        error,
        warning,
        info,
        env_vars,
        prompt_action,
        run_with_status,
    )
    from libs.deployer import Deployer, make_tasks
    from libs.config import Config

__all__ = [
    # env
    "get_secrets",
    "OpSecrets",
    "VaultSecrets",
    "generate_password",
    # common
    "get_env",
    "validate_env",
    "check_service",
    "CONTAINERS",
    # console
    "header",
    "success",
    "error",
    "warning",
    "info",
    "env_vars",
    "prompt_action",
    "run_with_status",
    # deployer
    "Deployer",
    "make_tasks",
    # config
    "Config",
]


def __getattr__(name):
    """Lazy imports to avoid circular dependencies"""
    if name in ("get_secrets", "OpSecrets", "VaultSecrets", "generate_password"):
        from libs import env

        return getattr(env, name)
    elif name in ("get_env", "validate_env", "check_service", "CONTAINERS"):
        from libs import common

        return getattr(common, name)
    elif name in (
        "header",
        "success",
        "error",
        "warning",
        "info",
        "env_vars",
        "prompt_action",
        "run_with_status",
        "console",
    ):
        from libs import console

        return getattr(console, name)
    elif name in ("Deployer", "make_tasks"):
        from libs import deployer

        return getattr(deployer, name)
    elif name == "Config":
        from libs import config

        return getattr(config, name)
    raise AttributeError(f"module 'libs' has no attribute {name!r}")
