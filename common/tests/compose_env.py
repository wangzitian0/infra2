"""Resolve what a compose service's container actually receives from a deploy's env.

A deploy pushes one compose-level env (``client.updated`` / ``env_updates``); each service's
``environment:`` block then picks values out of it with ``${VAR}`` / ``${VAR:-default}``.
Asserting on the pushed env alone proves the deploy issued a value, and asserting on the
compose text alone proves a reference exists; only resolving one against the other proves
the container gets it (the same gap truealpha#474's ``APP_HOST`` fell through).

Only the three substitution forms the repository's composes use are supported. Anything else
fails loudly, so a new form cannot silently resolve to an empty string.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from pathlib import Path

import yaml

_REFERENCE = re.compile(r"\$\{(?P<name>[A-Za-z_]\w*)(?P<default>:[-+][^${}]*)?\}")
# A reference whose name is followed by anything but `:-` or the closing brace (`:?`, `-`, `+`...).
_UNSUPPORTED = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?!:[-+]|\}|[A-Za-z0-9_])")
_OR_ENV_GROUP = re.compile(r'\bor((?:\s+\(env "[A-Za-z0-9_]+"\)){2,})')
TEMPLATE_ENV_CALL = re.compile(r'env "([A-Za-z0-9_]+)"')


def missing_template_envs(template: str, forwarded: Collection[str]) -> list[str]:
    """Return env references in ``template`` not satisfied by ``forwarded``.

    A Go template ``or (env "A") (env "B")`` fallback passes when at least one branch
    is forwarded; every standalone ``env "X"`` must be forwarded directly.
    """
    forwarded_set = set(forwarded)
    missing: list[str] = []
    for match in _OR_ENV_GROUP.finditer(template):
        branches = TEMPLATE_ENV_CALL.findall(match.group(1))
        if not any(name in forwarded_set for name in branches):
            missing.append("|".join(branches))
    remainder = _OR_ENV_GROUP.sub("", template)
    for name in sorted(set(TEMPLATE_ENV_CALL.findall(remainder))):
        if name not in forwarded_set:
            missing.append(name)
    return missing


def compose_services(compose_path: Path) -> dict[str, dict]:
    """The services of the deployed text: an x-infra2 file is compiled first (#1822)."""
    from common.deploy.compose_compile import load_stack

    return yaml.safe_load(load_stack(compose_path).compiled)["services"]


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
        default = match["default"]
        if default and default.startswith(":+"):
            # `${VAR:+text}`: text when VAR is set and non-empty, else empty.
            return default[2:] if issued else ""
        if issued:
            return issued
        return default[2:] if default else ""

    # Innermost reference first: `${A:-${B:-x}}` resolves `${B:-x}` first.
    previous = None
    while previous != value:
        previous, value = value, _REFERENCE.sub(_sub, value)
    return value


def container_env(
    compose_path: Path, service: str, issued_env: dict[str, str]
) -> dict[str, str]:
    """Every environment variable ``service`` is started with, given the deploy's env."""
    services = compose_services(compose_path)
    return {
        key: resolve(value, issued_env)
        for key, value in raw_environment(services[service]).items()
    }


def compiled_vault_template(directory: Path) -> str:
    """The Vault Agent template that the deploy ships for the stack ``directory``.

    An x-infra2 stack commits no ``secrets.ctmpl``: the compile writes the template inline
    into ``VAULT_SECRETS_TEMPLATE`` of its ``vault-agent`` sidecar (#1822, #1939). A
    plain stack still mounts the committed ``secrets.ctmpl``, which this function reads.
    """
    from common.deploy.compose_compile import has_x_infra2

    compose = directory / "compose.yaml"
    if not has_x_infra2(compose.read_text(encoding="utf-8")):
        return (directory / "secrets.ctmpl").read_text(encoding="utf-8")
    vault_agent = compose_services(compose).get("vault-agent") or {}
    inline = raw_environment(vault_agent).get("VAULT_SECRETS_TEMPLATE")
    assert inline is not None, f"{compose}: compiled x-infra2 stack lacks VAULT_SECRETS_TEMPLATE"
    return inline.replace("$$", "$")
