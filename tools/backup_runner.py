#!/usr/bin/env python3
"""Create inventory-backed backup archives and optional off-host uploads."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from libs.backup_verification import BackupEntry, load_backup_inventory


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="/tmp/infra2-backups")
    parser.add_argument("--remote", default=os.getenv("BACKUP_REMOTE", "r2:infra2"))
    parser.add_argument("--service", action="append", default=[])
    parser.add_argument("--no-upload", action="store_true")
    parser.add_argument("--manifest", default="")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    entries = _select_entries(load_backup_inventory(), set(args.service))
    timestamp = int(time.time())
    artifacts = []
    for entry in entries:
        archive = _archive_entry(entry, output_dir, timestamp)
        digest = _sha256(archive)
        remote_uri = f"{args.remote.rstrip('/')}/{entry.service_id}/{archive.name}"
        if not args.no_upload:
            _upload(archive, remote_uri)
        artifacts.append(
            {
                "service_id": entry.service_id,
                "created_at": timestamp,
                "size_bytes": archive.stat().st_size,
                "sha256": digest,
                "remote_uri": remote_uri if not args.no_upload else f"local:{archive}",
                "method": entry.method,
            }
        )

    manifest = {
        "schema_version": 1,
        "generated_at": timestamp,
        "verified_at": timestamp,
        "artifacts": artifacts,
    }
    manifest_path = Path(args.manifest) if args.manifest else output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(manifest_path)
    return 0


def _select_entries(entries: list[BackupEntry], selected: set[str]) -> list[BackupEntry]:
    if not selected:
        return entries
    known = {entry.service_id for entry in entries}
    unknown = selected - known
    if unknown:
        raise SystemExit(f"Unknown backup service(s): {', '.join(sorted(unknown))}")
    return [entry for entry in entries if entry.service_id in selected]


def _archive_entry(entry: BackupEntry, output_dir: Path, timestamp: int) -> Path:
    source = Path(entry.data_path)
    if not source.exists():
        raise SystemExit(f"Backup source is missing: {entry.service_id} {source}")
    archive_dir = output_dir / entry.service_id.replace("/", "_")
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_base = archive_dir / f"{timestamp}"
    return Path(
        shutil.make_archive(
            str(archive_base),
            "gztar",
            root_dir=source,
            base_dir=".",
        )
    )


def _upload(archive: Path, remote_uri: str) -> None:
    remote_dir = remote_uri.rsplit("/", 1)[0]
    result = subprocess.run(
        ["rclone", "copyto", str(archive), remote_uri],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or f"rclone upload failed: {remote_dir}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
