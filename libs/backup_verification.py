"""Backup inventory and freshness verification helpers."""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from libs.service_identity import ServiceIdentity


REPO_ROOT = Path(__file__).resolve().parents[1]


class BackupManifestError(ValueError):
    """Raised when a backup manifest field cannot be interpreted."""


@dataclass(frozen=True)
class BackupEntry:
    service_id: str
    data_path: str
    method: str
    restore_command: str
    remote: str
    retention_days: int
    rpo_hours: int


@dataclass(frozen=True)
class BackupCheck:
    service_id: str
    status: str
    severity: str
    summary: str
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Inventory-wide defaults, formerly the deleted handwritten YAML's `defaults:`
# block (#542). A BackupFacet leaving retention/rpo/remote at zero-values means
# "use these".
INVENTORY_DEFAULTS = {"retention_days": 30, "rpo_hours": 24, "remote": "r2"}


def load_backup_inventory(path: Path | str | None = None) -> list[BackupEntry]:
    """The backup inventory, DERIVED from each service's BackupFacet declarations.

    Formerly read from the handwritten ``ops.backup-inventory.yaml`` (deleted,
    #542 — frozen as a test fixture); now every entry derives from the owning
    Deployer via ``service_attrs()`` + ``bootstrap_facet_attrs()``:
    ``service_id``/``data_path`` come from the Deployer's registry identity and
    ``data_path`` attr unless the facet overrides them (bootstrap/1password and
    bootstrap/vault have no deploy.py; they are declared on iac_runner's, the
    bootstrap plane's single declaration point). ``path`` is accepted for
    YAML-fixture reads in tests (the equivalence anchor) — production callers
    pass nothing.
    """
    if path is not None:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        defaults = data.get("defaults", {})
        return [
            BackupEntry(**{**defaults, **raw_entry})
            for raw_entry in data.get("services", [])
        ]

    from libs.service_registry import bootstrap_facet_attrs, service_attrs

    attrs = {**service_attrs(), **bootstrap_facet_attrs()}
    entries: list[BackupEntry] = []
    seen: dict[str, str] = {}
    for owner_id in sorted(attrs):
        meta = attrs[owner_id]
        for facet in meta.backups:
            service_id = facet.service_id or owner_id
            data_path = facet.data_path or (meta.data_path or "")
            if not data_path:
                raise ValueError(
                    f"{owner_id}: BackupFacet needs a data_path (the Deployer "
                    "declares none and the facet does not override it)"
                )
            if service_id in seen:
                raise ValueError(
                    f"duplicate backup inventory id {service_id!r} declared by "
                    f"both {seen[service_id]} and {owner_id}"
                )
            seen[service_id] = owner_id
            entries.append(
                BackupEntry(
                    service_id=service_id,
                    data_path=data_path,
                    method=facet.method,
                    restore_command=facet.restore_command,
                    remote=facet.remote or INVENTORY_DEFAULTS["remote"],
                    retention_days=facet.retention_days
                    or INVENTORY_DEFAULTS["retention_days"],
                    rpo_hours=facet.rpo_hours or INVENTORY_DEFAULTS["rpo_hours"],
                )
            )
    return entries


def discover_deployer_data_paths(root: Path = REPO_ROOT) -> dict[str, str]:
    """Find deployer-owned persistent DATA_PATH roots from deploy.py files."""
    paths: dict[str, str] = {}
    for deploy_path in root.rglob("deploy.py"):
        if any(part.startswith(".") for part in deploy_path.parts):
            continue  # hidden dirs: .venv, .git, .claude worktrees (full repo copies)
        if ".venv" in deploy_path.parts:
            continue
        tree = ast.parse(deploy_path.read_text(encoding="utf-8"))
        data_path = _class_string_attr(tree, "data_path")
        if not data_path:
            continue
        service_id = _service_id_from_deploy_path(deploy_path.relative_to(root))
        paths[service_id] = data_path
    return paths


def inventory_data_paths(entries: list[BackupEntry]) -> dict[str, str]:
    return {entry.service_id: entry.data_path for entry in entries}


def verify_backup_manifest(
    entries: list[BackupEntry],
    manifest: dict[str, Any],
    *,
    now: int,
) -> dict[str, Any]:
    artifacts = {
        str(item.get("service_id")): item for item in manifest.get("artifacts", [])
    }
    checks: list[BackupCheck] = []
    for entry in entries:
        artifact = artifacts.get(entry.service_id)
        if not artifact:
            checks.append(
                _check(
                    entry,
                    "fail",
                    "P1",
                    "backup artifact is missing from manifest",
                    {},
                )
            )
            continue
        checks.append(_verify_artifact(entry, artifact, now=now))
    status = "pass" if all(check.status == "pass" for check in checks) else "fail"
    return {
        "schema_version": 1,
        "status": status,
        "generated_at": now,
        "checks": [check.to_dict() for check in checks],
    }


def build_backup_alert_payload(report: dict[str, Any]) -> dict[str, Any]:
    failures = [check for check in report["checks"] if check["status"] != "pass"]
    alerts = []
    for check in failures:
        identity = ServiceIdentity.build(
            check["service_id"],
            "production",
            component="backup",
        )
        alerts.append(
            {
                "status": "firing",
                "labels": {
                    "alertname": "InfraBackupVerificationFailed",
                    **identity.alert_labels(
                        severity=(
                            "critical"
                            if check["severity"] in {"P0", "P1"}
                            else check["severity"]
                        ),
                        failure_domain="backup",
                    ),
                },
                "annotations": {
                    "summary": check["summary"],
                    "description": str(check["evidence"]),
                },
            }
        )
    return {
        "status": "firing" if failures else "resolved",
        "commonLabels": {
            "alertname": "InfraBackupVerificationFailed",
            "identity_schema": "v1",
            "managed_by": "infra2",
            "severity": "critical" if failures else "info",
            "team": "infra",
        },
        "commonAnnotations": {
            "summary": f"{len(failures)} backup verification check(s) failed"
            if failures
            else "All backup verification checks passed",
        },
        "groupLabels": {"alertname": "InfraBackupVerificationFailed"},
        "alerts": alerts,
        "externalURL": "infra2://libs/backup_verification.py#load_backup_inventory",
    }


def _verify_artifact(
    entry: BackupEntry,
    artifact: dict[str, Any],
    *,
    now: int,
) -> BackupCheck:
    size = int(artifact.get("size_bytes") or 0)
    try:
        created_at = _parse_timestamp(artifact.get("created_at"))
    except BackupManifestError as exc:
        return _check(
            entry,
            "fail",
            "P1",
            "backup artifact timestamp is invalid",
            {"created_at": artifact.get("created_at"), "error": str(exc)},
        )
    age_hours = (now - created_at) / 3600
    checksum = str(artifact.get("sha256") or "")
    remote_uri = str(artifact.get("remote_uri") or "")
    evidence = {
        "age_hours": round(age_hours, 2),
        "size_bytes": size,
        "remote_uri": remote_uri,
        "sha256_present": bool(checksum),
    }
    if age_hours > entry.rpo_hours:
        return _check(entry, "fail", "P1", "backup artifact is stale", evidence)
    if size <= 0:
        return _check(entry, "fail", "P1", "backup artifact is empty", evidence)
    if not remote_uri.startswith(f"{entry.remote}:"):
        return _check(entry, "fail", "P1", "backup artifact is not off-host", evidence)
    if len(checksum) != 64:
        return _check(entry, "fail", "P1", "backup checksum is missing", evidence)
    return _check(
        entry, "pass", "P1", "backup artifact is fresh and verifiable", evidence
    )


def _check(
    entry: BackupEntry,
    status: str,
    severity: str,
    summary: str,
    evidence: dict[str, Any],
) -> BackupCheck:
    return BackupCheck(
        service_id=entry.service_id,
        status=status,
        severity=severity,
        summary=summary,
        evidence=evidence,
    )


def _class_string_attr(tree: ast.AST, attr_name: str) -> str | None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign):
                continue
            if not any(
                isinstance(target, ast.Name) and target.id == attr_name
                for target in stmt.targets
            ):
                continue
            if isinstance(stmt.value, ast.Constant) and isinstance(
                stmt.value.value, str
            ):
                return stmt.value.value
    return None


def _service_id_from_deploy_path(relative_path: Path) -> str:
    parts = relative_path.parts
    if parts[0] == "platform" and len(parts) >= 2:
        return f"platform/{parts[1].split('.', 1)[1]}"
    if parts[0] == "bootstrap" and len(parts) >= 2:
        return f"bootstrap/{parts[1].split('.', 1)[1].replace('-', '_')}"
    if parts[0] == "finance_report" and len(parts) >= 3:
        return f"finance_report/{parts[2].split('.', 1)[1]}"
    if parts[0] == "truealpha" and len(parts) >= 3:
        return f"truealpha/{parts[2].split('.', 1)[1]}"
    if parts[0] == "finance" and len(parts) >= 2:
        return f"finance/{parts[1]}"
    return "/".join(parts[:-1])


def _parse_timestamp(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if not isinstance(value, str) or not value.strip():
        raise BackupManifestError(
            "created_at must be a Unix timestamp or ISO-8601 string"
        )
    candidate = value.strip()
    if candidate.isdigit():
        return int(candidate)
    normalized = candidate.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise BackupManifestError(f"invalid created_at timestamp: {candidate}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.astimezone(timezone.utc).timestamp())
