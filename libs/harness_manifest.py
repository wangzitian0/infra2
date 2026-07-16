"""Read-only validation for the workspace harness repository inventory."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

SCHEMA_VERSION = 1
ALLOWED_CHECKOUTS = {"root", "submodule"}
ALLOWED_ROLES = {
    "infrastructure-control-plane",
    "cross-repository-contract",
    "external-application",
}
ALLOWED_GOVERNANCE = {"local", "coordinated", "autonomous"}


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


def validate_manifest(root: Path, manifest: dict[str, Any]) -> CheckResult:
    findings: list[Finding] = []
    if manifest.get("schema_version") != SCHEMA_VERSION:
        _error(
            findings,
            "schema-version",
            f"schema_version must be {SCHEMA_VERSION}",
        )

    workspace = manifest.get("workspace")
    if not isinstance(workspace, dict):
        _error(findings, "workspace", "workspace must be a mapping")
        workspace = {}
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

    required_strings = (
        "id",
        "path",
        "checkout",
        "role",
        "governance",
        "source",
        "release_identity",
    )
    for index, repository in enumerate(repositories):
        label = f"repositories[{index}]"
        if not isinstance(repository, dict):
            _error(findings, "repository", f"{label} must be a mapping")
            continue

        missing = [
            field
            for field in required_strings
            if not isinstance(repository.get(field), str) or not repository[field]
        ]
        if missing:
            _error(
                findings, "repository-fields", f"{label} missing: {', '.join(missing)}"
            )
            continue

        repository_id = repository["id"]
        relative = repository["path"]
        role = repository["role"]
        governance = repository["governance"]
        repository_ids.add(repository_id)
        governance_by_id[repository_id] = governance
        role_by_id[repository_id] = role
        if repository["checkout"] == "root":
            root_checkouts.append(repository_id)

        if repository_id in seen_ids:
            _error(
                findings, "duplicate-id", f"duplicate repository id: {repository_id}"
            )
        if relative in seen_paths:
            _error(findings, "duplicate-path", f"duplicate repository path: {relative}")
        seen_ids.add(repository_id)
        seen_paths.add(relative)

        if repository["checkout"] not in ALLOWED_CHECKOUTS:
            _error(findings, "checkout", f"{repository_id} has unsupported checkout")
        elif repository["checkout"] == "root" and relative != ".":
            _error(
                findings,
                "root-checkout",
                f"{repository_id} root checkout must use path '.'",
            )
        elif repository["checkout"] == "submodule" and relative == ".":
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
        expected_governance = {
            "infrastructure-control-plane": "local",
            "cross-repository-contract": "coordinated",
            "external-application": "autonomous",
        }.get(role)
        if expected_governance is not None and governance != expected_governance:
            _error(
                findings,
                "governance-role",
                f"{repository_id} role {role} requires governance {expected_governance}",
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

        checkout_path = _inside(root, relative)
        if checkout_path is None:
            _error(
                findings, "repository-path", f"{repository_id} escapes workspace root"
            )
            continue
        if not checkout_path.is_dir():
            findings.append(
                Finding(
                    "warning",
                    "checkout-missing",
                    f"{repository_id} checkout is not initialized: {relative}",
                )
            )
            continue
        for authority_path in authority:
            resolved = _inside(checkout_path, authority_path)
            if resolved is None or not resolved.is_file():
                _error(
                    findings,
                    "authority-path",
                    f"{repository_id} missing authority: {authority_path}",
                )

    if len(root_checkouts) != 1:
        _error(
            findings,
            "root-count",
            f"workspace must contain exactly one root checkout, found {len(root_checkouts)}",
        )

    focus = workspace.get("focus")
    if (
        not isinstance(focus, list)
        or not focus
        or not all(isinstance(item, str) for item in focus)
    ):
        _error(findings, "focus", "workspace.focus must be a non-empty string list")
    else:
        if len(focus) != len(set(focus)):
            _error(findings, "focus-duplicate", "workspace.focus contains duplicates")
        for repository_id in focus:
            if repository_id not in repository_ids:
                _error(
                    findings, "focus-id", f"unknown focus repository: {repository_id}"
                )
            elif (
                governance_by_id.get(repository_id) == "autonomous"
                or role_by_id.get(repository_id) == "external-application"
            ):
                _error(
                    findings,
                    "focus-autonomy",
                    f"autonomous repository cannot be workspace focus: {repository_id}",
                )

    return CheckResult(len(repositories), tuple(findings))


def check_workspace(root: Path, manifest_path: Path | None = None) -> CheckResult:
    path = manifest_path or root / "harness" / "repos.yaml"
    try:
        manifest = load_manifest(path)
    except HarnessManifestError as exc:
        return CheckResult(0, (Finding("error", "manifest-read", str(exc)),))
    return validate_manifest(root, manifest)
