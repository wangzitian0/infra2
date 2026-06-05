"""Backup inventory and freshness verification helpers."""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY_PATH = REPO_ROOT / "docs/ssot/ops.backup-inventory.yaml"


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


def load_backup_inventory(path: Path | str = DEFAULT_INVENTORY_PATH) -> list[BackupEntry]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    defaults = data.get("defaults", {})
    entries: list[BackupEntry] = []
    for raw_entry in data.get("services", []):
        merged = {**defaults, **raw_entry}
        entries.append(BackupEntry(**merged))
    return entries


def discover_deployer_data_paths(root: Path = REPO_ROOT) -> dict[str, str]:
    """Find deployer-owned persistent DATA_PATH roots from deploy.py files."""
    paths: dict[str, str] = {}
    for deploy_path in root.rglob("deploy.py"):
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
    return {
        "status": "firing" if failures else "resolved",
        "commonLabels": {
            "alertname": "InfraBackupVerificationFailed",
            "severity": "critical" if failures else "info",
            "team": "infra",
        },
        "commonAnnotations": {
            "summary": f"{len(failures)} backup verification check(s) failed"
            if failures
            else "All backup verification checks passed",
        },
        "groupLabels": {"alertname": "InfraBackupVerificationFailed"},
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "InfraBackupVerificationFailed",
                    "service": check["service_id"],
                    "severity": check["severity"],
                },
                "annotations": {
                    "summary": check["summary"],
                    "description": str(check["evidence"]),
                },
            }
            for check in failures
        ],
        "externalURL": "infra2://docs/ssot/ops.backup-inventory.yaml",
    }


def _verify_artifact(
    entry: BackupEntry,
    artifact: dict[str, Any],
    *,
    now: int,
) -> BackupCheck:
    size = int(artifact.get("size_bytes") or 0)
    created_at = _parse_timestamp(str(artifact.get("created_at") or ""))
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
    return _check(entry, "pass", "P1", "backup artifact is fresh and verifiable", evidence)


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
            if not any(isinstance(target, ast.Name) and target.id == attr_name for target in stmt.targets):
                continue
            if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
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
    if parts[0] == "finance" and len(parts) >= 2:
        return f"finance/{parts[1]}"
    return "/".join(parts[:-1])


def _parse_timestamp(value: str) -> int:
    if value.isdigit():
        return int(value)
    normalized = value.replace("Z", "+00:00")
    return int(datetime.fromisoformat(normalized).astimezone(timezone.utc).timestamp())
