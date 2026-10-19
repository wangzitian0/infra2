"""The deployment environment of a probe process: one resolver, no default (#1208).

Four modules used to resolve the environment alone, with different variable names and
different precedence. Three of them fell back to production when nothing was set. A
probe that cannot tell its environment and reports itself as production writes to the
wrong ledger and the wrong alert stream.

``resolve_probe_environment`` is the only reader of these variables. It raises when no
variable holds a value. It raises when the value names no known environment. It never
returns production by default.

This module needs no SDK. The watchdog-style jobs that install few packages can import it.
"""

from __future__ import annotations

import os

from common.core.environ import EnvironmentNotSetError, normalize_env_name

DEFAULT_ALERTING_ENV_FILE = "/secrets/.env"

__all__ = [
    "DEFAULT_ALERTING_ENV_FILE",
    "PROBE_ENVIRONMENT_VARIABLES",
    "resolve_probe_environment",
]

# The variables that choose the environment, in precedence order. The first variable
# with a non-blank value decides. Real deployments set these:
#   ENV                the alerting probe container (compose.yaml)
#   DEPLOY_ENV         workflows and the iac-runner child environment
#   INFRA_ENVIRONMENT  the signed service identity, and the iac-runner
# All of them hold the same value in one process, so their order only decides a
# conflict that no deployment creates.
PROBE_ENVIRONMENT_VARIABLES = (
    "INFRA_ENVIRONMENT",
    "ENV",
    "DEPLOY_ENV",
)


def resolve_probe_environment(*overrides: str) -> str:
    """Return the normalized environment name of this process.

    ``overrides`` name extra variables that rank before the shared ones. The round-trip
    probe passes ``OBS_ROUNDTRIP_ENV`` here.

    Blank values count as unset. ``prod`` becomes ``production``, ``stg`` becomes
    ``staging``, and a preview slot such as ``pr-5`` becomes ``pr_5``. The result is
    ``production``, ``staging``, ``preview`` or a preview slot.

    Raises ``EnvironmentNotSetError`` when no variable holds a value. Raises
    ``ValueError`` when the first variable that holds a value names no known environment.
    A bad value does not fall through to a later variable: that would hide a typo.
    """
    names = (*overrides, *PROBE_ENVIRONMENT_VARIABLES)
    for name in names:
        raw = (os.environ.get(name) or "").strip()
        if raw:
            return _known_environment(name, raw, names)
    raise EnvironmentNotSetError(
        f"No probe environment is set. Set one of {', '.join(names)}, "
        "for example ENV=staging. "
        "Production is never the default: choose it with ENV=production."
    )


def _known_environment(name: str, raw: str, names: tuple[str, ...]) -> str:
    # Imported here: common.alerting loads more code than a bare name check needs.
    from common.alerting import is_report_only_environment

    try:
        environment = normalize_env_name(raw)
    except ValueError as exc:
        raise ValueError(_unknown_message(name, raw, names)) from exc
    if environment != "production" and not is_report_only_environment(environment):
        raise ValueError(_unknown_message(name, raw, names))
    return environment


def _unknown_message(name: str, raw: str, names: tuple[str, ...]) -> str:
    return (
        f"{name}={raw!r} names no known environment. "
        "Use production, staging, preview, or a preview slot such as pr-5. "
        f"The variables read, in order, are: {', '.join(names)}."
    )
