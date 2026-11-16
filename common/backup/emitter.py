"""Backup runner inventory emitter.

Projects declared BackupFacet inventory entries into platform-specific
execution targets for host backup runners.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from common.backup.verification import BackupEntry, load_backup_inventory

LEDGER_SERVICE_ID = "observability/availability_ledger"
LEDGER_FILENAME = "availability-ledger.json"


@dataclass(frozen=True)
class HostBackupTarget:
    """Platform execution target for one backup inventory entry."""

    service_id: str
    kind: str  # pg, redis, or path
    primary_target: str
    secondary_target: str = ""

    def to_runner_line(self) -> str:
        """Render the target to the pipe-delimited runner specification."""
        if self.secondary_target:
            return f"{self.service_id}|{self.kind}|{self.primary_target}|{self.secondary_target}"
        return f"{self.service_id}|{self.kind}|{self.primary_target}"


class DokployDockerAdapter:
    """Resolves backup entries for Dokploy Docker container environments."""

    def __init__(self, data_root: str = "/data", suffix: str = "") -> None:
        self.data_root = data_root.rstrip("/")
        self.suffix = suffix

    def resolve(self, entry: BackupEntry) -> HostBackupTarget:
        """Resolve a single BackupEntry into a HostBackupTarget."""
        is_bootstrap = entry.service_id.startswith("bootstrap/")
        project, service = entry.service_id.split("/", 1)
        env_suffix = "" if is_bootstrap else self.suffix

        # Resolve storage path
        raw_path = entry.data_path
        if raw_path.startswith("/data"):
            data_path = self.data_root + raw_path[len("/data") :]
        else:
            data_path = raw_path
        data_path = f"{data_path}{env_suffix}"

        container = f"{project}-{service}{env_suffix}"

        if entry.method.startswith("pg_dump"):
            return HostBackupTarget(
                service_id=entry.service_id,
                kind="pg",
                primary_target=container,
            )
        if entry.method.startswith("redis"):
            return HostBackupTarget(
                service_id=entry.service_id,
                kind="redis",
                primary_target=container,
                secondary_target=data_path,
            )
        return HostBackupTarget(
            service_id=entry.service_id,
            kind="path",
            primary_target=data_path,
        )


def _kind_priority(target: HostBackupTarget) -> int:
    """Return sorting order: pg first, redis second, path third."""
    if target.kind == "pg":
        return 0
    if target.kind == "redis":
        return 1
    # Place s3 last among path backups because it is the largest archive.
    if target.service_id == "platform/s3":
        return 3
    return 2


def emit_backup_targets(
    entries: Sequence[BackupEntry] | None = None,
    data_root: str = "/data",
    environment: str = "production",
    suffix: str | None = None,
    ledger_dir: str | Path | None = None,
) -> list[HostBackupTarget]:
    """Emit sorted host backup targets from the backup inventory."""
    if entries is None:
        entries = load_backup_inventory(environment=environment)

    if suffix is None:
        suffix = "-staging" if environment == "staging" else ""

    adapter = DokployDockerAdapter(data_root=data_root, suffix=suffix)
    targets = [adapter.resolve(entry) for entry in entries]
    if ledger_dir is not None and str(ledger_dir).strip():
        ledger_path = Path(str(ledger_dir).strip())
        if (ledger_path / LEDGER_FILENAME).is_file():
            targets.append(
                HostBackupTarget(
                    service_id=LEDGER_SERVICE_ID,
                    kind="path",
                    primary_target=str(ledger_path),
                )
            )
        elif ledger_path.is_dir():
            print(
                f"WARN {LEDGER_SERVICE_ID}: directory {ledger_path} exists "
                f"without {LEDGER_FILENAME}; skipping archive",
                file=sys.stderr,
            )
    targets.sort(key=lambda t: (_kind_priority(t), t.service_id))
    return targets


def emit_runner_lines(
    entries: Sequence[BackupEntry] | None = None,
    data_root: str = "/data",
    environment: str = "production",
    suffix: str | None = None,
    ledger_dir: str | Path | None = None,
) -> list[str]:
    """Emit pipe-delimited runner lines for host backup scripts."""
    targets = emit_backup_targets(
        entries=entries,
        data_root=data_root,
        environment=environment,
        suffix=suffix,
        ledger_dir=ledger_dir,
    )
    return [target.to_runner_line() for target in targets]


def main() -> int:
    """CLI entrypoint for host backup runner integration."""
    parser = argparse.ArgumentParser(description="Emit backup inventory runner lines.")
    parser.add_argument(
        "--environment",
        choices=["production", "staging"],
        default="production",
        help="Deployment environment name.",
    )
    parser.add_argument(
        "--data-root",
        default="/data",
        help="Host persistent data root directory.",
    )
    parser.add_argument(
        "--suffix",
        default=None,
        help="Explicit container and path suffix (e.g. -staging).",
    )
    parser.add_argument(
        "--ledger-dir",
        default=None,
        help="Host availability ledger directory.",
    )
    args = parser.parse_args()

    effective_suffix = (
        args.suffix
        if args.suffix is not None
        else ("-staging" if args.environment == "staging" else "")
    )
    default_ledger = Path(
        f"{os.environ.get('BACKUP_LEDGER_ROOT', '/var/lib/infra2-availability-ledger')}{effective_suffix}"
    )
    ledger_dir = (
        args.ledger_dir
        if args.ledger_dir is not None
        else (
            str(default_ledger)
            if (default_ledger / LEDGER_FILENAME).is_file()
            else None
        )
    )
    lines = emit_runner_lines(
        data_root=args.data_root,
        environment=args.environment,
        suffix=args.suffix,
        ledger_dir=ledger_dir,
    )
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
