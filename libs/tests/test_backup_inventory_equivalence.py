"""#542 backup-inventory migration equivalence proof — PERMANENT regression anchor.

``libs/tests/fixtures/backup_inventory_frozen.yaml`` is a byte-copy of the
handwritten ``the ops.backup-inventory YAML`` as it stood before the
BackupFacet migration (the YAML itself is deleted). The derived inventory
(every service's BackupFacet declarations -> ``load_backup_inventory()``) must
stay field-for-field equal to it. Deliberate inventory changes update the
fixture in the same PR, making the change explicit and reviewable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from libs.backup_verification import load_backup_inventory

ROOT = Path(__file__).resolve().parents[2]
FROZEN = ROOT / "libs/tests/fixtures/backup_inventory_frozen.yaml"


def test_derived_inventory_equals_frozen_yaml_field_by_field() -> None:
    derived = sorted(load_backup_inventory(), key=lambda e: e.service_id)
    frozen = sorted(load_backup_inventory(FROZEN), key=lambda e: e.service_id)
    assert [e.service_id for e in derived] == [e.service_id for e in frozen]
    assert derived == frozen  # every field, not just ids


def test_handwritten_yaml_stays_deleted() -> None:
    # resurrection guard: the derived loader is the ONLY source now
    assert not (ROOT / "the ops.backup-inventory YAML").exists()


def test_duplicate_backup_ids_fail_closed(monkeypatch) -> None:
    import dataclasses

    import libs.service_registry as reg
    from libs.service_facets import BackupFacet

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

    import libs.service_registry as reg
    from libs.service_facets import BackupFacet

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
