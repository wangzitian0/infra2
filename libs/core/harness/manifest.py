"""Read-only validation for the workspace harness repository inventory."""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

Runner = Callable[..., subprocess.CompletedProcess]

SCHEMA_VERSION = 1
ALLOWED_CHECKOUTS = {"root", "submodule"}
ALLOWED_ROLES = {
    "infrastructure-control-plane",
    "cross-repository-contract",
    "workspace-tooling",
    "external-application",
}
ALLOWED_GOVERNANCE = {"local", "coordinated", "autonomous"}
ALLOWED_CONTRACTS = {"pinned", "snapshot"}
# "none" is the only defined value today: a checkout so declared does not carry a
# Repo-layer rules projection (no AGENTS.md/CLAUDE.md expected there) — its
# constraints travel through a published artifact and its own README/pyproject
# instead (core.harness.md §1; dev_env #51). Absent the key, the default stays
# "a Repo layer exists", so this is opt-out, not opt-in.
ALLOWED_RULES_LAYER = {"none"}


def _git(
    checkout: Path,
    *args: str,
    runner: Runner = subprocess.run,
) -> subprocess.CompletedProcess:
    return runner(
        ["git", "-C", str(checkout), *args],
        capture_output=True,
        text=True,
    )


def _value(
    checkout: Path,
    *args: str,
    runner: Runner = subprocess.run,
) -> str | None:
    completed = _git(checkout, *args, runner=runner)
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def _parent_pin(
    root: Path, relative: str, *, runner: Runner = subprocess.run
) -> str | None:
    value = _value(root, "ls-tree", "HEAD", "--", relative, runner=runner)
    if not value:
        return None
    metadata = value.split("\t", 1)[0].split()
    return metadata[2].lower() if len(metadata) >= 3 else None


class HarnessManifestError(ValueError):
    """Raised when the inventory cannot be decoded as a mapping."""


@dataclass(frozen=True)
class Finding:
    level: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class CheckResult:
    repository_count: int
    findings: tuple[Finding, ...]

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.level == "error")

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.level == "warning")

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "repository_count": self.repository_count,
            "errors": [item.to_dict() for item in self.errors],
            "warnings": [item.to_dict() for item in self.warnings],
        }


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise HarnessManifestError(f"cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise HarnessManifestError(f"{path} must contain a YAML mapping")
    return raw


def _inside(base: Path, relative: str) -> Path | None:
    candidate = (base / relative).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError:
        return None
    return candidate


def _error(findings: list[Finding], code: str, message: str) -> None:
    findings.append(Finding("error", code, message))


def _validate_workspace_section(
    root: Path, workspace: Any, findings: list[Finding]
) -> dict[str, Any]:
    if not isinstance(workspace, dict):
        _error(findings, "workspace", "workspace must be a mapping")
        return {}
    if not isinstance(workspace.get("id"), str) or not workspace.get("id"):
        _error(findings, "workspace-id", "workspace.id must be a non-empty string")

    preferences = workspace.get("preferences", [])
    if not isinstance(preferences, list) or not all(
        isinstance(item, str) for item in preferences
    ):
        _error(findings, "preferences", "workspace.preferences must be a string list")
    else:
        for relative in preferences:
            path = _inside(root, relative)
            if path is None or not path.is_file():
                _error(findings, "preference-path", f"missing preference: {relative}")
    return workspace


def _validate_repository_entry(
    root: Path,
    repository: Any,
    index: int,
    findings: list[Finding],
    submodules_expected: bool,
    runner: Runner,
) -> tuple[str, str, str, str] | None:
    label = f"repositories[{index}]"
    if not isinstance(repository, dict):
        _error(findings, "repository", f"{label} must be a mapping")
        return None

    required_strings = (
        "id",
        "path",
        "checkout",
        "role",
        "governance",
        "source",
        "release_identity",
    )
    missing = [
        field
        for field in required_strings
        if not isinstance(repository.get(field), str) or not repository[field]
    ]
    if missing:
        _error(findings, "repository-fields", f"{label} missing: {', '.join(missing)}")
        return None

    repository_id = repository["id"]
    relative = repository["path"]
    checkout = repository["checkout"]
    role = repository["role"]
    governance = repository["governance"]

    if checkout not in ALLOWED_CHECKOUTS:
        _error(findings, "checkout", f"{repository_id} has unsupported checkout")
    elif checkout == "root" and relative != ".":
        _error(
            findings,
            "root-checkout",
            f"{repository_id} root checkout must use path '.'",
        )
    elif checkout == "submodule" and relative == ".":
        _error(
            findings,
            "submodule-checkout",
            f"{repository_id} submodule checkout cannot use path '.'",
        )

    if role not in ALLOWED_ROLES:
        _error(findings, "role", f"{repository_id} has unsupported role: {role}")
    if governance not in ALLOWED_GOVERNANCE:
        _error(
            findings,
            "governance",
            f"{repository_id} has unsupported governance: {governance}",
        )

    rules_layer = repository.get("rules_layer")
    if rules_layer is not None and (
        not isinstance(rules_layer, str) or rules_layer not in ALLOWED_RULES_LAYER
    ):
        _error(
            findings,
            "rules-layer",
            f"{repository_id} has unsupported rules_layer: {rules_layer!r}",
        )

    contract = repository.get("contract")
    if contract is not None:
        if not isinstance(contract, str) or contract not in ALLOWED_CONTRACTS:
            _error(
                findings,
                "contract",
                f"{repository_id} has unsupported contract: {contract!r}",
            )
            contract = "snapshot" if role == "external-application" else "pinned"
    else:
        contract = "snapshot" if role == "external-application" else "pinned"

    expected_gov = {
        "infrastructure-control-plane": "local",
        "cross-repository-contract": "coordinated",
        "workspace-tooling": "coordinated",
        "external-application": "autonomous",
    }.get(role)
    if expected_gov is not None and governance != expected_gov:
        _error(
            findings,
            "governance-role",
            f"{repository_id} role {role} requires governance {expected_gov}",
        )

    authority = repository.get("authority")
    if (
        not isinstance(authority, list)
        or not authority
        or not all(isinstance(item, str) for item in authority)
    ):
        _error(
            findings,
            "authority",
            f"{repository_id} authority must be a non-empty string list",
        )
        authority = []

    is_optional = bool(repository.get("optional", False))
    uninitialized_level = (
        "warning"
        if is_optional or (checkout == "submodule" and not submodules_expected)
        else "error"
    )

    checkout_path = _inside(root, relative)
    if checkout_path is None:
        _error(findings, "repository-path", f"{repository_id} escapes workspace root")
        return repository_id, relative, checkout, role

    if not checkout_path.is_dir():
        findings.append(
            Finding(
                uninitialized_level,
                "checkout-missing",
                f"{repository_id} checkout is not initialized: {relative}",
            )
        )
        return repository_id, relative, checkout, role

    if checkout == "submodule" and not (checkout_path / ".git").exists():
        findings.append(
            Finding(
                uninitialized_level,
                "checkout-uninitialized",
                f"{repository_id} submodule is not initialized: {relative}",
            )
        )
        return repository_id, relative, checkout, role

    if (
        checkout == "submodule"
        and (checkout_path / ".git").exists()
        and submodules_expected
    ):
        parent_pin = _parent_pin(root, relative, runner=runner)
        head = _value(checkout_path, "rev-parse", "HEAD^{commit}", runner=runner)
        if parent_pin and head and parent_pin != head.lower():
            if contract == "pinned":
                findings.append(
                    Finding(
                        "error",
                        "pin-drift",
                        f"{repository_id} pinned submodule has drifted: checkout={head[:12]} parent={parent_pin[:12]}",
                    )
                )
            elif contract == "snapshot":
                findings.append(
                    Finding(
                        "warning",
                        "pin-drift",
                        f"{repository_id} snapshot submodule has advanced: checkout={head[:12]} parent={parent_pin[:12]}",
                    )
                )

    for authority_path in authority:
        resolved = _inside(checkout_path, authority_path)
        if resolved is None or not resolved.is_file():
            _error(
                findings,
                "authority-path",
                f"{repository_id} missing authority: {authority_path}",
            )

    return repository_id, relative, checkout, role


def _validate_workspace_focus(
    workspace: dict[str, Any],
    repository_ids: set[str],
    governance_by_id: dict[str, str],
    role_by_id: dict[str, str],
    findings: list[Finding],
) -> None:
    focus = workspace.get("focus")
    if (
        not isinstance(focus, list)
        or not focus
        or not all(isinstance(item, str) for item in focus)
    ):
        _error(findings, "focus", "workspace.focus must be a non-empty string list")
        return
    if len(focus) != len(set(focus)):
        _error(findings, "focus-duplicate", "workspace.focus contains duplicates")
    for repository_id in focus:
        if repository_id not in repository_ids:
            _error(findings, "focus-id", f"unknown focus repository: {repository_id}")
        elif (
            governance_by_id.get(repository_id) == "autonomous"
            or role_by_id.get(repository_id) == "external-application"
        ):
            _error(
                findings,
                "focus-autonomy",
                f"autonomous repository cannot be workspace focus: {repository_id}",
            )


def validate_manifest(
    root: Path,
    manifest: dict[str, Any],
    *,
    submodules_expected: bool = True,
    runner: Runner = subprocess.run,
) -> CheckResult:
    findings: list[Finding] = []
    if manifest.get("schema_version") != SCHEMA_VERSION:
        _error(
            findings,
            "schema-version",
            f"schema_version must be {SCHEMA_VERSION}",
        )

    workspace = _validate_workspace_section(root, manifest.get("workspace"), findings)

    repositories = manifest.get("repositories")
    if not isinstance(repositories, list):
        _error(findings, "repositories", "repositories must be a list")
        return CheckResult(0, tuple(findings))

    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    repository_ids: set[str] = set()
    governance_by_id: dict[str, str] = {}
    role_by_id: dict[str, str] = {}
    root_checkouts: list[str] = []

    for index, repository in enumerate(repositories):
        res = _validate_repository_entry(
            root=root,
            repository=repository,
            index=index,
            findings=findings,
            submodules_expected=submodules_expected,
            runner=runner,
        )
        if res is None:
            continue
        repository_id, relative, checkout, role = res
        repository_ids.add(repository_id)
        governance_by_id[repository_id] = repository.get("governance", "")
        role_by_id[repository_id] = role
        if checkout == "root":
            root_checkouts.append(repository_id)

        if repository_id in seen_ids:
            _error(
                findings, "duplicate-id", f"duplicate repository id: {repository_id}"
            )
        if relative in seen_paths:
            _error(findings, "duplicate-path", f"duplicate repository path: {relative}")
        seen_ids.add(repository_id)
        seen_paths.add(relative)

    if len(root_checkouts) != 1:
        _error(
            findings,
            "root-count",
            f"workspace must contain exactly one root checkout, found {len(root_checkouts)}",
        )

    _validate_workspace_focus(
        workspace, repository_ids, governance_by_id, role_by_id, findings
    )

    return CheckResult(len(repositories), tuple(findings))


def check_workspace(
    root: Path,
    manifest_path: Path | None = None,
    *,
    submodules_expected: bool = True,
    runner: Runner = subprocess.run,
) -> CheckResult:
    path = manifest_path or root / "harness" / "repos.yaml"
    try:
        manifest = load_manifest(path)
    except HarnessManifestError as exc:
        return CheckResult(0, (Finding("error", "manifest-read", str(exc)),))
    return validate_manifest(
        root, manifest, submodules_expected=submodules_expected, runner=runner
    )


__all__ = [
    "ALLOWED_CHECKOUTS",
    "ALLOWED_CONTRACTS",
    "ALLOWED_GOVERNANCE",
    "ALLOWED_ROLES",
    "ALLOWED_RULES_LAYER",
    "CheckResult",
    "Finding",
    "HarnessManifestError",
    "Runner",
    "SCHEMA_VERSION",
    "check_workspace",
    "load_manifest",
    "validate_manifest",
]
