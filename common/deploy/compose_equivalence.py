"""PR-time equivalence of compiled ``x-infra2`` compose files (infra2#1822 Slice 1).

The check reads every tracked compose file at HEAD and does two things:

1. A plain file (no ``x-infra2``) must compile to the same bytes. This proves that the
   compile step changes nothing that a plain stack deploys.
2. A file that is ``x-infra2`` at HEAD and plain at the base ref is a migration. The
   check compiles it and compares the result with the hand-written file at the base
   ref. Only the normalizations below are allowed; any other difference fails.

Normalizations (the intended differences, from the infra2-sdk#238 design):

- ``vault-agent``: the sidecar reads its config and template from environment
  variables, not from mounted files. The check drops ``volumes``, ``labels``,
  ``entrypoint`` and the ``VAULT_AGENT_HCL`` / ``VAULT_SECRETS_TEMPLATE`` values, after
  it proves that the template equals the base ``secrets.ctmpl`` and the config equals
  the base ``vault-agent.hcl`` (``/etc/vault/`` written as ``/vault/``). It compares
  the healthcheck script by code lines. It drops the base env entry
  ``VAULT_SECRETS_TEMPLATE: ${VAULT_SECRETS_TEMPLATE:-}`` (the release template that the
  app promote path injects); the promote path compares that template with the compiled one.
  The compiled sidecar sets ``ENVIRONMENT: ${ENV}`` and no ``ENV`` (#1823 N1), so
  a base sidecar that sets ``ENV`` is a difference.
- An ``x-vault`` service: the hand entrypoint ends in ``exec <command>``; the compiled
  one runs ``exec "$@"`` with ``command``. The check proves both run the same process,
  then drops ``entrypoint`` and ``command``.
- Labels compare as a sorted list. A list-form ``environment`` compares as a mapping.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from common.core.constants import REPO_ROOT
from common.deploy.compose_compile import (
    ComposeSourceError,
    compile_stack,
    has_x_infra2,
    vault_template_for,
)
from common.deploy.yaml_text import safe_load_text

_GIT_TIMEOUT = 60
_SIDECAR = "vault-agent"
_SIDECAR_DROPPED = ("volumes", "labels", "entrypoint")
_SIDECAR_ENV_DROPPED = ("VAULT_AGENT_HCL", "VAULT_SECRETS_TEMPLATE")
_RELEASE_TEMPLATE_INJECTION = "${VAULT_SECRETS_TEMPLATE:-}"


def _yaml():
    import yaml

    return yaml


def tracked_compose_files(root: Path = REPO_ROOT) -> list[str]:
    """Every tracked compose file, the same set as `git ls-files '*compose*.y*ml'`."""
    proc = subprocess.run(
        ["git", "ls-files", "*compose*.yaml", "*compose*.yml"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
        check=True,
    )
    return sorted(line for line in proc.stdout.splitlines() if line.strip())


def text_at_ref(ref: str, path: str, root: Path = REPO_ROOT) -> str | None:
    """The file text at ``ref``, or None when the file does not exist there.

    An unknown ``ref`` raises ``ValueError``: a missing base must not skip the comparison
    (review of #1870)."""

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=False,
        )

    if git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode != 0:
        raise ValueError(f"base ref {ref!r} is not a commit in {root}")
    if git("cat-file", "-e", f"{ref}:{path}").returncode != 0:
        return None
    proc = git("show", f"{ref}:{path}")
    if proc.returncode != 0:
        raise ValueError(f"git show {ref}:{path} failed: {proc.stderr.strip()[:200]}")
    return proc.stdout


def _code_lines(script: str) -> list[str]:
    return [
        line.rstrip()
        for line in script.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _labels(value: Any) -> list[str]:
    if isinstance(value, dict):
        return sorted(f"{k}={v}" for k, v in value.items())
    return sorted(str(v) for v in value or [])


def _environment(value: Any) -> Any:
    if isinstance(value, list):
        out: dict[str, Any] = {}
        for item in value:
            key, _, val = str(item).partition("=")
            out[key] = val
        return out
    return value


def _diff(a: Any, b: Any, where: str, out: list[str]) -> None:
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b), key=str):
            sub = f"{where}.{key}" if where else str(key)
            if key not in a:
                out.append(f"{sub}: only in the base file")
            elif key not in b:
                out.append(f"{sub}: only in the compiled file")
            else:
                _diff(a[key], b[key], sub, out)
    elif a != b:
        out.append(f"{where}: compiled {a!r} != base {b!r}")


@dataclass
class _Pair:
    compiled: dict[str, Any]
    base: dict[str, Any]
    problems: list[str] = field(default_factory=list)


def _normalize_sidecar(pair: _Pair, base_files: dict[str, str | None]) -> None:
    compiled = pair.compiled["services"].get(_SIDECAR)
    hand = pair.base["services"].get(_SIDECAR)
    if not isinstance(compiled, dict) or not isinstance(hand, dict):
        return
    env = compiled.get("environment") or {}
    template = base_files.get("secrets.ctmpl")
    got = str(env.get("VAULT_SECRETS_TEMPLATE", "")).replace("$$", "$")
    if template is None:
        pair.problems.append(
            "vault-agent: the base ref has no secrets.ctmpl to compare"
        )
    elif got != template:
        pair.problems.append(
            "vault-agent: the compiled template differs from the base secrets.ctmpl"
        )
    hcl = base_files.get("vault-agent.hcl")
    got_hcl = _code_lines(str(env.get("VAULT_AGENT_HCL", "")).replace("$$", "$"))
    if hcl is None:
        pair.problems.append(
            "vault-agent: the base ref has no vault-agent.hcl to compare"
        )
    elif got_hcl != _code_lines(hcl.replace("/etc/vault/", "/vault/")):
        pair.problems.append(
            "vault-agent: the compiled config differs from the base vault-agent.hcl"
        )
    for spec in (compiled, hand):
        health = spec.get("healthcheck")
        if isinstance(health, dict) and isinstance(health.get("test"), list):
            health["test"] = [
                _code_lines(x) if isinstance(x, str) and "\n" in x else x
                for x in health["test"]
            ]
        for key in _SIDECAR_DROPPED:
            spec.pop(key, None)
    for key in _SIDECAR_ENV_DROPPED:
        env.pop(key, None)
    # An app sidecar takes the release template from the deploy as base64 (#1822 Slice 3).
    # The compiled sidecar embeds the template, proven equal above; rollout
    # stops when the release template differs (deploy_v2).
    hand_env = hand.get("environment")
    if (
        isinstance(hand_env, dict)
        and hand_env.get("VAULT_SECRETS_TEMPLATE") == _RELEASE_TEMPLATE_INJECTION
    ):
        hand_env.pop("VAULT_SECRETS_TEMPLATE")


def _normalize_x_vault(pair: _Pair, name: str) -> None:
    compiled = pair.compiled["services"].get(name)
    hand = pair.base["services"].get(name)
    if not isinstance(compiled, dict) or not isinstance(hand, dict):
        return
    command = compiled.get("command")
    entry = hand.get("entrypoint")
    script = entry[2] if isinstance(entry, list) and len(entry) > 2 else ""
    lines = _code_lines(str(script))
    if not isinstance(command, list):
        pair.problems.append(
            f"services.{name}: compiled x-vault service has no command"
        )
    elif hand.get("command") is not None:
        if hand.get("command") != command:
            pair.problems.append(
                f"services.{name}: command {command!r} != base {hand.get('command')!r}"
            )
    elif not lines or lines[-1] != "exec " + " ".join(str(x) for x in command):
        pair.problems.append(
            f"services.{name}: the base entrypoint does not end in "
            f"`exec {' '.join(str(x) for x in command)}`"
        )
    for spec in (compiled, hand):
        spec.pop("entrypoint", None)
        spec.pop("command", None)


def equivalence_problems(
    source_text: str,
    compiled_text: str,
    base_text: str,
    base_files: dict[str, str | None],
) -> list[str]:
    """Differences between a compiled x-infra2 file and its hand-written base file.

    ``base_files`` maps ``secrets.ctmpl`` and ``vault-agent.hcl`` to their text at the
    base ref (None when absent). An empty list means equivalent.
    """
    yaml = _yaml()
    # safe_load_text returns a deep copy, so the normalizations below cannot leak.
    source = safe_load_text(yaml, source_text)
    pair = _Pair(safe_load_text(yaml, compiled_text), safe_load_text(yaml, base_text))
    for doc, label in ((pair.compiled, "compiled"), (pair.base, "base")):
        if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict):
            return [f"the {label} file has no `services` mapping"]
    _normalize_sidecar(pair, base_files)
    for name, spec in (source.get("services") or {}).items():
        if isinstance(spec, dict) and "x-vault" in spec:
            _normalize_x_vault(pair, name)
    for doc in (pair.compiled, pair.base):
        for spec in doc["services"].values():
            if not isinstance(spec, dict):
                continue
            if "labels" in spec:
                spec["labels"] = _labels(spec["labels"])
            if "environment" in spec:
                spec["environment"] = _environment(spec["environment"])
    _diff(pair.compiled, pair.base, "", pair.problems)
    return pair.problems


def _mode_for(path: str) -> str:
    """``preview`` for a preview stack file (`preview/compose.yaml`, `preview.compose.yaml`)."""
    p = Path(path)
    return "preview" if p.parent.name == "preview" or "preview" in p.name else "deploy"


@dataclass
class CheckReport:
    files: int = 0
    plain_identical: int = 0
    x_infra2: int = 0
    compared: int = 0
    problems: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"compose files: {self.files}; plain files byte-identical after compile: "
            f"{self.plain_identical}; x-infra2 files: {self.x_infra2}; "
            f"migrations compared with the base file: {self.compared}; "
            f"problems: {len(self.problems)}"
        )


def check(base_ref: str, root: Path = REPO_ROOT) -> CheckReport:
    report = CheckReport()
    # A base that git cannot read is a problem even when no file has migrated yet.
    try:
        text_at_ref(base_ref, "compose.yaml", root)
    except ValueError as exc:
        report.problems.append(str(exc))
        return report
    for path in tracked_compose_files(root):
        report.files += 1
        text = (root / path).read_text(encoding="utf-8")
        if not has_x_infra2(text):
            if compile_stack(text) == text:
                report.plain_identical += 1
            else:
                report.problems.append(f"{path}: a plain file changed in the compile")
            continue
        report.x_infra2 += 1
        mode = _mode_for(path)
        try:
            compiled = compile_stack(
                text,
                vault_template=vault_template_for(path, text, mode=mode),
                mode=mode,
            )
        except ComposeSourceError as exc:
            report.problems.append(f"{path}: {exc}")
            continue
        base_text = text_at_ref(base_ref, path, root)
        if base_text is None or has_x_infra2(base_text):
            # A new stack, or one migrated earlier: no hand-written base to compare.
            continue
        directory = Path(path).parent.as_posix()
        base_files = {
            name: text_at_ref(base_ref, f"{directory}/{name}", root)
            for name in ("secrets.ctmpl", "vault-agent.hcl")
        }
        report.compared += 1
        report.problems.extend(
            f"{path}: {p}"
            for p in equivalence_problems(text, compiled, base_text, base_files)
        )
    if report.files == 0:
        report.problems.append("no compose file was checked; the scan is empty")
    return report


__all__ = [
    "CheckReport",
    "check",
    "equivalence_problems",
    "text_at_ref",
    "tracked_compose_files",
]
