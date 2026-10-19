"""Resolve what a compose service's container actually receives from a deploy's env.

A deploy pushes one compose-level env (``client.updated`` / ``env_updates``); each service's
``environment:`` block then picks values out of it with ``${VAR}`` / ``${VAR:-default}``.
Asserting on the pushed env alone proves the deploy issued a value, and asserting on the
compose text alone proves a reference exists; only resolving one against the other proves
the container gets it (the same gap truealpha#474's ``APP_HOST`` fell through).

Only the two substitution forms the repository's composes use are supported. Anything else
fails loudly, so a new form cannot silently resolve to an empty string.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REFERENCE = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<default>:-[^}]*)?\}")
# A reference whose name is followed by anything but `:-` or the closing brace (`:?`, `-`, `+`...).
_UNSUPPORTED = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?!:-|\}|[A-Za-z0-9_])")


def compose_services(compose_path: Path) -> dict[str, dict]:
    return yaml.safe_load(compose_path.read_text(encoding="utf-8"))["services"]


def raw_environment(service: dict) -> dict[str, str]:
    """The service's ``environment:`` as written (list or mapping form), unresolved."""
    env = service.get("environment") or {}
    if isinstance(env, list):
        return dict(item.split("=", 1) for item in env)
    return {str(key): str(value) for key, value in env.items()}


def resolve(value: str, issued_env: dict[str, str]) -> str:
    """Resolve ``${VAR}`` / ``${VAR:-default}`` against ``issued_env`` (unset or empty
    takes the default, as ``:-`` does in Compose)."""
    assert not _UNSUPPORTED.search(value), (
        f"unsupported compose substitution in {value!r}"
    )

    def _sub(match: re.Match[str]) -> str:
        issued = issued_env.get(match["name"], "")
        if issued:
            return issued
        default = match["default"]
        return default[2:] if default else ""

    return _REFERENCE.sub(_sub, value)


def container_env(
    compose_path: Path, service: str, issued_env: dict[str, str]
) -> dict[str, str]:
    """Every environment variable ``service`` is started with, given the deploy's env."""
    services = compose_services(compose_path)
    return {
        key: resolve(value, issued_env)
        for key, value in raw_environment(services[service]).items()
    }
