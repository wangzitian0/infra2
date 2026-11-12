"""Purge expired host quarantine batches (docs/ssot/ops.storage.md, SOP-003).

Usage: python3 quarantine_purge.py <quarantine_dir> <now_epoch>

Two callers use this file as the only definition of the purge rule:
- `host_quarantine.sh --purge-expired` runs it from the checkout;
- `tools/host_hygiene_schedule.py` embeds its text in the scheduled host script.

A batch is purged when it holds at least one `quarantined` record, every
`quarantined` record is expired, and every other record is already `purged` or
`archived_to_gdrive_and_purged`. A record with any other status keeps the batch.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys

ARCHIVED = "archived_to_gdrive_and_purged"


def purge_expired(quarantine_dir: pathlib.Path, now_epoch: int) -> list[str]:
    """Delete each expired batch directory and mark its records `purged`."""
    quarantine_dir = quarantine_dir.resolve()
    manifest_path = quarantine_dir / "manifest.jsonl"
    if not manifest_path.is_file():
        return []
    lines = manifest_path.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines if line.strip()]
    batches: dict[str, list[dict]] = {}
    for rec in records:
        batches.setdefault(rec["batch_id"], []).append(rec)

    purged: set[str] = set()
    refused: list[str] = []
    for batch_id, recs in batches.items():
        pending = [r for r in recs if r.get("status") == "quarantined"]
        known = all(
            r.get("status") in ("quarantined", "purged", ARCHIVED) for r in recs
        )
        if not pending or not known:
            continue
        if any(int(r["purge_after_epoch"]) > now_epoch for r in pending):
            continue
        # A batch id is one plain directory name. A path, a symlink, a name that
        # resolves outside the quarantine directory, a file or a name the operating
        # system rejects is refused. A failure on one batch never stops the other
        # batches: they still run, and the run exits non-zero at the end.
        try:
            batch_dir = quarantine_dir / batch_id
            if (
                not batch_id
                or pathlib.PurePath(batch_id).name != batch_id
                or batch_dir.is_symlink()
                or batch_dir.resolve().parent != quarantine_dir
                or (batch_dir.exists() and not batch_dir.is_dir())
            ):
                refused.append(batch_id)
                continue
            if batch_dir.exists():
                shutil.rmtree(batch_dir)
        except (OSError, ValueError):
            refused.append(batch_id)
            continue
        purged.add(batch_id)

    if purged:
        for rec in records:
            if rec["batch_id"] in purged and rec.get("status") == "quarantined":
                rec["status"] = "purged"
                rec["purged_at_epoch"] = now_epoch
        tmp_path = manifest_path.with_suffix(".jsonl.tmp")
        tmp_path.write_text(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in records),
            encoding="utf-8",
        )
        os.chmod(tmp_path, 0o600)
        tmp_path.replace(manifest_path)
    for batch_id in sorted(purged):
        print(f"purged quarantine batch {batch_id}")
    if refused:
        raise SystemExit(
            "refusing to purge quarantine batch(es) that are not plain directories "
            "inside the quarantine dir: " + ", ".join(repr(b) for b in refused)
        )
    return sorted(purged)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(
            "usage: quarantine_purge.py <quarantine_dir> <now_epoch>", file=sys.stderr
        )
        return 2
    purge_expired(pathlib.Path(argv[0]), int(argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
