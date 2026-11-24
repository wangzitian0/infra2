"""#542 backup-inventory derivation tests: fail-closed rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from common.backup.verification import load_backup_inventory

ROOT = Path(__file__).resolve().parents[2]


def test_handwritten_yaml_stays_deleted() -> None:
    # resurrection guard: the derived loader is the ONLY source now
    assert not (ROOT / "the ops.backup-inventory YAML").exists()


def test_duplicate_backup_ids_fail_closed(monkeypatch) -> None:
    import dataclasses

    import common.core.registry as reg
    from common.core.facets import BackupFacet

    real_attrs = dict(reg.service_attrs())
    donor = real_attrs["platform/postgres"]
    clash = BackupFacet(
        service_id="platform/redis",  # already declared by platform/redis itself
        data_path="/data/x",
        method="x",
    )
    real_attrs["platform/postgres"] = dataclasses.replace(
        donor, backups=donor.backups + (clash,)
    )
    monkeypatch.setattr(reg, "service_attrs", lambda: dict(real_attrs))
    with pytest.raises(ValueError, match="duplicate backup inventory id"):
        load_backup_inventory()


def test_missing_data_path_fails_closed(monkeypatch) -> None:
    import dataclasses

    import common.core.registry as reg
    from common.core.facets import BackupFacet

    real_attrs = dict(reg.service_attrs())
    donor = real_attrs["platform/postgres"]
    real_attrs["platform/postgres"] = dataclasses.replace(
        donor,
        data_path=None,
        backups=(BackupFacet(method="x"),),
    )
    monkeypatch.setattr(reg, "service_attrs", lambda: dict(real_attrs))
    with pytest.raises(ValueError, match="needs a data_path"):
        load_backup_inventory()
