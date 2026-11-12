"""Purge rule of expired host quarantine batches (#1624, docs/ssot/ops.storage.md SOP-003)."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

SOURCE = (
    Path(__file__).resolve().parents[2]
    / "bootstrap/01.dokploy_install/host_guard/quarantine_purge.py"
)
_spec = importlib.util.spec_from_file_location("quarantine_purge", SOURCE)
assert _spec and _spec.loader
qp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qp)

NOW = 1_800_000_000
EXPIRED = NOW - 100
LIVE = NOW + 86_400


def _record(batch: str, status: str, purge_after: int, name: str = "f") -> dict:
    return {
        "batch_id": batch,
        "original_path": f"/root/{name}",
        "purge_after_epoch": purge_after,
        "quarantine_path": f"/q/{batch}/files/root/{name}",
        "status": status,
    }


def _setup(tmp_path: Path, records: list[dict]) -> Path:
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    for batch in {r["batch_id"] for r in records}:
        (quarantine / batch / "files").mkdir(parents=True)
        (quarantine / batch / "files" / "payload").write_text("x", encoding="utf-8")
    (quarantine / "manifest.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )
    return quarantine


def _statuses(quarantine: Path) -> dict[tuple[str, str], int]:
    out: dict[tuple[str, str], int] = {}
    for line in (
        (quarantine / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    ):
        rec = json.loads(line)
        key = (rec["batch_id"], rec["status"])
        out[key] = out.get(key, 0) + 1
    return out


def test_an_expired_batch_is_purged_and_its_records_are_marked(tmp_path: Path) -> None:
    quarantine = _setup(tmp_path, [_record("b1", "quarantined", EXPIRED)])
    assert qp.purge_expired(quarantine, NOW) == ["b1"]
    assert not (quarantine / "b1").exists()
    assert _statuses(quarantine) == {("b1", "purged"): 1}
    rec = json.loads((quarantine / "manifest.jsonl").read_text(encoding="utf-8"))
    assert rec["purged_at_epoch"] == NOW


def test_a_batch_with_an_already_archived_record_is_purged(tmp_path: Path) -> None:
    """2026-11-12: batch 20261027T061347Z held 8 archived and 33 expired records and
    the old rule (every record must be `quarantined`) never purged it."""
    records = [
        _record("b1", "archived_to_gdrive_and_purged", EXPIRED, "a"),
        _record("b1", "quarantined", EXPIRED, "b"),
        _record("b1", "quarantined", EXPIRED, "c"),
    ]
    quarantine = _setup(tmp_path, records)
    assert qp.purge_expired(quarantine, NOW) == ["b1"]
    assert not (quarantine / "b1").exists()
    # the archived record keeps its own status; only quarantined records become purged
    assert _statuses(quarantine) == {
        ("b1", "archived_to_gdrive_and_purged"): 1,
        ("b1", "purged"): 2,
    }


def test_one_unexpired_record_keeps_the_whole_batch(tmp_path: Path) -> None:
    records = [
        _record("b1", "quarantined", EXPIRED, "a"),
        _record("b1", "quarantined", LIVE, "b"),
    ]
    quarantine = _setup(tmp_path, records)
    assert qp.purge_expired(quarantine, NOW) == []
    assert (quarantine / "b1" / "files" / "payload").exists()
    assert _statuses(quarantine) == {("b1", "quarantined"): 2}


def test_a_record_with_an_unknown_status_keeps_the_batch(tmp_path: Path) -> None:
    records = [
        _record("b1", "quarantined", EXPIRED, "a"),
        _record("b1", "restore_requested", EXPIRED, "b"),
    ]
    quarantine = _setup(tmp_path, records)
    assert qp.purge_expired(quarantine, NOW) == []
    assert (quarantine / "b1").exists()


def test_the_purge_date_itself_is_expired(tmp_path: Path) -> None:
    quarantine = _setup(tmp_path, [_record("b1", "quarantined", NOW)])
    assert qp.purge_expired(quarantine, NOW) == ["b1"]


def test_only_the_expired_batch_is_purged(tmp_path: Path) -> None:
    records = [
        _record("old", "quarantined", EXPIRED),
        _record("new", "quarantined", LIVE),
    ]
    quarantine = _setup(tmp_path, records)
    assert qp.purge_expired(quarantine, NOW) == ["old"]
    assert not (quarantine / "old").exists()
    assert (quarantine / "new").exists()
    assert _statuses(quarantine) == {("old", "purged"): 1, ("new", "quarantined"): 1}


def test_a_batch_without_a_pending_record_is_left_alone(tmp_path: Path) -> None:
    quarantine = _setup(tmp_path, [_record("b1", "purged", EXPIRED)])
    before = (quarantine / "manifest.jsonl").read_text(encoding="utf-8")
    assert qp.purge_expired(quarantine, NOW) == []
    assert (quarantine / "manifest.jsonl").read_text(encoding="utf-8") == before


def test_a_missing_manifest_is_not_an_error(tmp_path: Path) -> None:
    assert qp.purge_expired(tmp_path, NOW) == []


def test_a_batch_id_that_leaves_the_quarantine_directory_is_refused(
    tmp_path: Path,
) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    (quarantine / "manifest.jsonl").write_text(
        json.dumps(_record("../victim", "quarantined", EXPIRED)) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="outside quarantine dir"):
        qp.purge_expired(quarantine, NOW)
    assert victim.exists()


def test_the_manifest_is_rewritten_atomically_with_mode_0600(tmp_path: Path) -> None:
    quarantine = _setup(tmp_path, [_record("b1", "quarantined", EXPIRED)])
    qp.purge_expired(quarantine, NOW)
    manifest = quarantine / "manifest.jsonl"
    assert stat.S_IMODE(os.stat(manifest).st_mode) == 0o600
    assert not (quarantine / "manifest.jsonl.tmp").exists()


def test_the_command_line_reports_each_purged_batch(tmp_path: Path, capsys) -> None:
    quarantine = _setup(tmp_path, [_record("b1", "quarantined", EXPIRED)])
    assert qp.main([str(quarantine), str(NOW)]) == 0
    assert "purged quarantine batch b1" in capsys.readouterr().out


def test_the_command_line_refuses_wrong_arguments(capsys) -> None:
    assert qp.main(["only-one"]) == 2
    assert "usage" in capsys.readouterr().err
