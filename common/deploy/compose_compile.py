"""Compile a stack compose file with infra2-sdk and decide its Dokploy source type (#1822).

A compose file that has a top-level ``x-infra2`` key is a short source. The deploy
compiles it with ``infra2_sdk.rules.compile_compose`` into plain Docker Compose YAML.
A compose file without that key is a plain file. For a plain file, ``compile_stack``
returns the input text unchanged (the same bytes). The SDK compiler cannot do this
itself: it requires ``x-infra2`` and it writes the YAML again in its own format.

Rules:

1. The deploy hashes the compiled text (``CONFIGURATION_SHA256``), never the source.
2. A plain file keeps ``sourceType="github"``. Its bytes, its hash and its upload do
   not change.
3. A compiled stack uses ``sourceType="raw"`` only when the compiled text has no
   ``build:`` and no bind mount relative to the checkout. A ``raw`` record has no
   checkout, so such a stack cannot run. Dokploy with ``github`` runs the source file
   from the checkout and ignores the compile, so an ``x-infra2`` stack that is not
   raw-eligible stops the deploy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from common.core.constants import REPO_ROOT
from common.deploy.config_hash import (
    _bind_mount_sources,
    _expand_compose_env_defaults,
)
from common.deploy.yaml_text import safe_load_text

X_INFRA2 = "x-infra2"
SOURCE_GITHUB = "github"
SOURCE_RAW = "raw"
MODES = ("deploy", "preview")


class ComposeSourceError(ValueError):
    """The compose source cannot be compiled, or its compiled text cannot deploy."""


def _yaml():
    import yaml

    return yaml


def has_x_infra2(text: str) -> bool:
    """True when the compose text is a YAML mapping with a top-level ``x-infra2`` key.

    Text that is not valid YAML is a plain file: the deploy keeps its old path, and the
    compose validation in CI reports the parse error.
    """
    if X_INFRA2 not in text:
        return False
    yaml = _yaml()
    try:
        doc = safe_load_text(yaml, text)
    except yaml.YAMLError:
        return False
    return isinstance(doc, dict) and X_INFRA2 in doc


def compile_stack(
    text: str, *, vault_template: str | None = None, mode: str = "deploy"
) -> str:
    """The compose text that the deploy uploads and hashes.

    A plain file returns ``text`` itself (byte-identical pass-through). An ``x-infra2``
    file returns ``compile_compose(text, vault_template=..., mode=...)``.
    """
    if mode not in MODES:
        raise ComposeSourceError(f"mode must be one of {MODES}, not {mode!r}")
    if not has_x_infra2(text):
        return text
    # Imported here: only an x-infra2 stack needs the SDK 4.x compiler (infra2#1822).
    from infra2_sdk.rules import ComposeCompileError, compile_compose

    try:
        return compile_compose(text, vault_template=vault_template, mode=mode)
    except ComposeCompileError as exc:
        raise ComposeSourceError(f"x-infra2 compile failed: {exc}") from exc


def _needs_vault_template(text: str) -> bool:
    doc = safe_load_text(_yaml(), text)
    block = doc.get(X_INFRA2) if isinstance(doc, dict) else None
    return isinstance(block, dict) and block.get("vault") is not None


def compiles_a_vault_sidecar(text: str) -> bool:
    """True when the compiled stack of ``text`` runs a ``vault-agent`` sidecar.

    A plain file declares the service ``vault-agent`` itself (at two-space indent). An
    ``x-infra2`` file gets the sidecar from the compiler when it sets ``x-infra2.vault``.
    """
    if has_x_infra2(text):
        return _needs_vault_template(text)
    return re.search(r"(?m)^  vault-agent:", text) is not None


def _registry_service(compose_path: str | Path):
    """The one ``common.security.registry`` service whose directory holds the stack."""
    from common.security.registry import SERVICES

    path = Path(compose_path)
    if path.is_absolute():
        path = path.resolve().relative_to(REPO_ROOT)
    directory = path.parent.as_posix()
    matches = [s for s in SERVICES if s.directory == directory]
    if len(matches) != 1:
        raise ComposeSourceError(
            f"{path}: x-infra2.vault needs exactly one registry service for "
            f"{directory!r}, found {len(matches)}"
        )
    return matches[0]


def vault_manifest_paths(compose_path: str | Path, text: str) -> tuple[str, ...]:
    """The repository-relative manifests that the Vault template of the stack reads.

    A plain file or a stack without ``x-infra2.vault`` reads none.
    """
    if not has_x_infra2(text) or not _needs_vault_template(text):
        return ()
    return tuple(_registry_service(compose_path).manifests)


def vault_template_for(
    compose_path: str | Path, text: str, *, manifest_root: Path | None = None
) -> str | None:
    """The rendered Vault Agent template for an ``x-infra2`` stack with ``vault``.

    The template comes from the stack manifest through ``common.security.secrets_render``,
    the same renderer that writes ``secrets.ctmpl`` today. ``manifest_root`` holds the
    manifests (default: the checkout); the config-drift check passes the manifests of a
    release ref. A plain file or a stack without ``x-infra2.vault`` returns ``None``.
    """
    if not has_x_infra2(text) or not _needs_vault_template(text):
        return None
    from common.security.secrets_render import render

    service = _registry_service(compose_path)
    return render(service, root=manifest_root)["secrets.ctmpl"]


# The compiled vault-agent sidecar (infra2_sdk.rules.compose, `x-vault`) writes its
# template to this path, and labels itself with the SHA-256 of the template text.
COMPILED_TEMPLATE_PATH = "/vault/secrets.ctmpl"
VAULT_SIDECAR = "vault-agent"
VAULT_TEMPLATE_LABEL = "infra2.vault.template.sha256"


def vault_template_label(compiled_text: str) -> str:
    """The ``infra2.vault.template.sha256`` label of the compiled vault-agent sidecar.

    Returns ``""`` when the compiled text has no sidecar or no such label.
    """
    doc = safe_load_text(_yaml(), compiled_text)
    services = doc.get("services") if isinstance(doc, dict) else None
    sidecar = services.get(VAULT_SIDECAR) if isinstance(services, dict) else None
    labels = sidecar.get("labels") if isinstance(sidecar, dict) else None
    if isinstance(labels, dict):
        value = labels.get(VAULT_TEMPLATE_LABEL)
        return value if isinstance(value, str) else ""
    for label in labels if isinstance(labels, list) else ():
        key, sep, value = str(label).partition("=")
        if sep and key == VAULT_TEMPLATE_LABEL:
            return value
    return ""


def _is_host_relative(source: str) -> bool:
    """True when a bind mount source resolves inside the checkout.

    ``./x``, ``../x``, ``dir/x`` and ``${VAR:-./x}`` are relative. An absolute path, a
    ``${VAR}/x`` host path and a named volume (no ``/``) are not.
    """
    expanded = _expand_compose_env_defaults(source)
    if expanded is None:
        # An unresolved `${VAR}` prefix is a host path variable (`${DATA_PATH}/x`).
        return False
    if not expanded or expanded.startswith(("/", "~")):
        return False
    return expanded.startswith(".") or "/" in expanded


def raw_ineligibility(compiled_text: str) -> list[str]:
    """The reasons why a compiled stack cannot deploy as ``sourceType="raw"``.

    An empty list means the stack is raw-eligible. Text that cannot be read gives a
    reason, so it is never raw-eligible.
    """
    yaml = _yaml()
    try:
        doc = safe_load_text(yaml, compiled_text)
    except yaml.YAMLError as exc:
        return [f"compiled text is not valid YAML: {exc}"]
    services = doc.get("services") if isinstance(doc, dict) else None
    if not isinstance(services, dict) or not services:
        return ["compiled text has no `services` mapping"]
    reasons: list[str] = []
    for name, spec in services.items():
        if not isinstance(spec, dict):
            reasons.append(f"services.{name} is not a mapping")
            continue
        if "build" in spec:
            reasons.append(
                f"services.{name} has `build:`; raw has no checkout to build"
            )
        for source in _bind_mount_sources(spec):
            if _is_host_relative(source):
                reasons.append(
                    f"services.{name} mounts {source!r} from the checkout; raw has no checkout"
                )
    for block in ("configs", "secrets"):
        entries = doc.get(block)
        if not isinstance(entries, dict):
            continue
        for name, entry in entries.items():
            file = entry.get("file") if isinstance(entry, dict) else None
            if isinstance(file, str) and _is_host_relative(file):
                reasons.append(f"{block}.{name} reads {file!r} from the checkout")
    return reasons


def is_raw_eligible(compiled_text: str) -> bool:
    return not raw_ineligibility(compiled_text)


@dataclass(frozen=True)
class StackCompose:
    """The deploy view of one stack compose file."""

    source: str
    compiled: str
    x_infra2: bool

    @property
    def source_type(self) -> str:
        """``github`` for a plain file; ``raw`` for a raw-eligible x-infra2 stack."""
        if not self.x_infra2:
            return SOURCE_GITHUB
        reasons = raw_ineligibility(self.compiled)
        if reasons:
            raise ComposeSourceError(
                "an x-infra2 stack must be raw-eligible; with sourceType=github Dokploy "
                "runs the uncompiled source. " + "; ".join(reasons)
            )
        return SOURCE_RAW


def stack_from_text(
    compose_path: str | Path, text: str, *, mode: str = "deploy"
) -> StackCompose:
    """The deploy view of compose ``text`` read from ``compose_path``."""
    compiled = compile_stack(
        text, vault_template=vault_template_for(compose_path, text), mode=mode
    )
    return StackCompose(source=text, compiled=compiled, x_infra2=has_x_infra2(text))


def load_stack(compose_path: str | Path, *, mode: str = "deploy") -> StackCompose:
    """Read a stack compose file (repository-relative or absolute) and compile it."""
    path = Path(compose_path)
    disk = path if path.is_absolute() else REPO_ROOT / path
    return stack_from_text(compose_path, disk.read_text(encoding="utf-8"), mode=mode)


__all__ = [
    "COMPILED_TEMPLATE_PATH",
    "ComposeSourceError",
    "MODES",
    "SOURCE_GITHUB",
    "SOURCE_RAW",
    "StackCompose",
    "X_INFRA2",
    "compile_stack",
    "compiles_a_vault_sidecar",
    "has_x_infra2",
    "is_raw_eligible",
    "load_stack",
    "raw_ineligibility",
    "stack_from_text",
    "VAULT_SIDECAR",
    "VAULT_TEMPLATE_LABEL",
    "vault_manifest_paths",
    "vault_template_for",
    "vault_template_label",
]
