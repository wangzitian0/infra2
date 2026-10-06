"""Hand-written service tables must agree with the Deployer registry (#1023, from #955).

`libs.core.registry.ServiceMeta` (read by AST from each `deploy.py`) is the root fact
about a service. Two tables still repeat parts of it by hand:

* `libs.security.registry.SERVICES` repeats project, service and directory for every
  service that takes secrets;
* the app rows of `libs.deploy.contract.SERVICES` repeat the Deployer's rollout state
  and telemetry name.

Nothing failed when one copy moved and the other did not. These tests make each copy
fail on drift, in both directions, and name the few rows that differ on purpose.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from libs.core import registry
from libs.deploy import contract
from libs.security.registry import SERVICES as SECRET_ROWS

# Rows whose Vault / 1Password scope name is not the registry service name. Renaming the
# Vault path is a migration of production secrets, not a table edit (#1023).
SCOPE_NAME_OVERRIDES = {"platform/03.s3": "minio"}


def _deployers() -> dict[str, registry.ServiceMeta]:
    """Deployer directory -> its registry entry (platform, apps and bootstrap)."""
    metas = {**registry.service_attrs(), **registry.bootstrap_facet_attrs()}
    return {
        str(Path(meta.compose_path).parent): meta
        for meta in metas.values()
        if meta.compose_path
    }


def _fixed_rows():
    return [row for row in SECRET_ROWS if not row.preview]


def test_the_tables_are_not_empty() -> None:
    assert len(_fixed_rows()) >= 10
    assert len(_deployers()) >= 15


@pytest.mark.parametrize("row", _fixed_rows(), ids=lambda row: row.directory)
def test_each_secrets_row_names_a_deployer_and_agrees_with_it(row) -> None:
    meta = _deployers().get(row.directory)
    assert meta is not None, (
        f"secrets row {row.directory!r} names no Deployer: the service moved or was "
        "removed, and libs/security/registry.py did not follow"
    )
    assert row.project == meta.project, (row.directory, row.project, meta.project)
    expected = SCOPE_NAME_OVERRIDES.get(row.directory, meta.service)
    assert row.service == expected, (row.directory, row.service, expected)


def test_every_deployer_that_declares_secrets_has_one_secrets_row() -> None:
    rows: dict[str, int] = {}
    for row in _fixed_rows():
        rows[row.directory] = rows.get(row.directory, 0) + 1
    missing = sorted(
        directory
        for directory, meta in _deployers().items()
        if meta.secrets and rows.get(directory) != 1
    )
    assert not missing, (
        f"Deployers with a SecretsFacet but not exactly one secrets row: {missing}"
    )


def test_scope_name_overrides_are_still_needed() -> None:
    """An override whose row now matches the registry is dead text: delete it."""
    deployers = _deployers()
    for directory, scope in SCOPE_NAME_OVERRIDES.items():
        assert deployers[directory].service != scope, directory


@pytest.mark.parametrize("key", sorted(contract.SERVICES))
def test_app_spec_rollout_state_comes_from_the_deployer(key, monkeypatch) -> None:
    """The app specs do not hand-copy prod_only / not_yet_in_production: flipping the
    Deployer's attribute must flip what service_spec() reports."""
    real = registry.service_attrs()
    flipped = replace(
        real[key],
        prod_only=not real[key].prod_only,
        not_yet_in_production=not real[key].not_yet_in_production,
    )
    monkeypatch.setattr(registry, "service_attrs", lambda: {**real, key: flipped})
    spec = contract.service_spec(key)
    assert spec.prod_only == flipped.prod_only
    assert spec.not_yet_in_production == flipped.not_yet_in_production


@pytest.mark.parametrize("key", sorted(contract.SERVICES))
def test_app_spec_identity_name_matches_the_deployer_telemetry_name(key) -> None:
    meta = registry.service_attrs()[key]
    assert contract.service_spec(key).identity_service_name == (
        meta.telemetry_service_name
    ), key
