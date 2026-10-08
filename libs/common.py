"""Operator task helper for deploy tasks: ``check_service``.

``check_service`` runs a health command over SSH and prints through ``libs.console``.
A domain package must not import ``libs.console``, so the helper stays in this flat module.

This module offers nothing else. The environment helpers live in ``libs.core.environ``.
Import them from there (#1164).
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from libs.core import environ

if TYPE_CHECKING:
    from invoke import Context

__all__ = ["check_service"]


def check_service(c: "Context", service: str, health_cmd: str) -> dict:
    """Check if a Docker service is healthy.

    Args:
        service: Either a key from CONTAINERS mapping or a full container name (e.g., 'finance_report-postgres')
        health_cmd: Command to run inside container to check health

    Returns:
        dict with is_ready and details keys
    """
    from libs.console import error, success

    env = environ.get_env()

    if service in environ.CONTAINERS:
        container = environ.CONTAINERS[service]
    elif "-" in service:
        container = service
    else:
        container = f"platform-{service}"

    container = environ.with_env_suffix(container, env)

    # Build the local and remote shells independently.  Health commands often
    # contain their own quotes (python -c + URLs); interpolating them inside one
    # outer single-quoted SSH string silently changes the command and produces a
    # false-unhealthy result.  ``sh -lc`` preserves the intended command while
    # shlex.quote protects both shell boundaries.
    remote_command = shlex.join(["docker", "exec", container, "sh", "-lc", health_cmd])
    command = shlex.join(["ssh", f"root@{env['VPS_HOST']}", remote_command])
    result = c.run(command, warn=True, hide=True)

    if result.ok:
        success(f"{container}: ready")
        return {"is_ready": True, "details": "Healthy"}

    error(f"{container}: not ready")
    return {"is_ready": False, "details": "Unhealthy"}
