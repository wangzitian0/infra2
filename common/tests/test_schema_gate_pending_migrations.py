"""The pre-deploy schema gate tolerates enum types that a pending migration creates (#1658).

Evidence: soak run 43360538840 of infra2 v3.1.58 blocked finance_report v0.2.65 because migration
0074 creates four enum types. The gate compares the image's enum types with the live database
before the image's entrypoint runs `alembic upgrade head`, so a new type is always missing.
"""

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from tools import pre_deploy_schema_check as gate
from tools.pre_deploy_schema_check import EnumDiscrepancy, cover_with_pending_migrations


def _migration(versions: Path, revision: str, down: str | None, body: str = "") -> None:
    down_literal = "None" if down is None else repr(down)
    versions.joinpath(f"{revision}.py").write_text(
        f'revision: str = "{revision}"\ndown_revision: str | None = {down_literal}\n{body}\n',
        encoding="utf-8",
    )


def _missing(name: str, *labels: str) -> EnumDiscrepancy:
    return EnumDiscrepancy(
        enum_name=name,
        code_values=labels,
        db_values=(),
        missing_in_db=labels,
        missing_in_code=(),
        casing_mismatches=(),
    )


CREATE_KIND = 'ENUM("extract", "classify", name="statement_run_kind_enum")'


@pytest.fixture
def versions(tmp_path: Path) -> Path:
    directory = tmp_path / "versions"
    directory.mkdir()
    _migration(directory, "0073", None)
    return directory


def test_a_missing_type_that_a_pending_migration_names_does_not_block(
    versions: Path,
) -> None:
    _migration(versions, "0074", "0073", CREATE_KIND)
    blocking, covered = cover_with_pending_migrations(
        [_missing("statement_run_kind_enum", "extract", "classify")],
        applied_heads=("0073",),
        versions_dir=versions,
    )
    assert blocking == []
    assert covered == [("statement_run_kind_enum", "0074")]


def test_a_missing_type_that_no_pending_migration_names_still_blocks(
    versions: Path,
) -> None:
    _migration(versions, "0074", "0073", CREATE_KIND)
    other = _missing("another_enum", "extract", "classify")
    blocking, covered = cover_with_pending_migrations(
        [other], applied_heads=("0073",), versions_dir=versions
    )
    assert blocking == [other]
    assert covered == []


def test_a_label_that_the_pending_migration_does_not_name_still_blocks(
    versions: Path,
) -> None:
    _migration(versions, "0074", "0073", CREATE_KIND)
    discrepancy = _missing("statement_run_kind_enum", "extract", "classify", "backfill")
    blocking, covered = cover_with_pending_migrations(
        [discrepancy], applied_heads=("0073",), versions_dir=versions
    )
    assert blocking == [discrepancy]
    assert covered == []


def test_a_label_added_to_an_existing_type_is_covered_by_its_alter_statement(
    versions: Path,
) -> None:
    _migration(
        versions,
        "0074",
        "0073",
        "op.execute(\"ALTER TYPE statement_run_kind_enum ADD VALUE 'backfill'\")",
    )
    blocking, covered = cover_with_pending_migrations(
        [_missing("statement_run_kind_enum", "backfill")],
        applied_heads=("0073",),
        versions_dir=versions,
    )
    assert blocking == []
    assert covered == [("statement_run_kind_enum", "0074")]


def test_a_migration_that_is_already_applied_does_not_cover_a_missing_type(
    versions: Path,
) -> None:
    """The database ran 0074 and still lacks the type: that is a defect, not a pending change."""
    _migration(versions, "0074", "0073", CREATE_KIND)
    discrepancy = _missing("statement_run_kind_enum", "extract", "classify")
    blocking, covered = cover_with_pending_migrations(
        [discrepancy], applied_heads=("0074",), versions_dir=versions
    )
    assert blocking == [discrepancy]
    assert covered == []


@pytest.mark.parametrize(
    "discrepancy",
    [
        EnumDiscrepancy(
            "statement_run_kind_enum",
            ("extract",),
            ("extract", "gone"),
            (),
            ("gone",),
            (),
        ),
        EnumDiscrepancy(
            "statement_run_kind_enum",
            ("EXTRACT",),
            ("extract",),
            ("EXTRACT",),
            (),
            (("EXTRACT", "extract"),),
        ),
        EnumDiscrepancy(
            "statement_run_kind_enum",
            (),
            ("extract", "classify"),
            (),
            ("extract", "classify"),
            (),
        ),
    ],
)
def test_a_db_only_label_and_a_casing_drift_still_block(
    versions: Path, discrepancy: EnumDiscrepancy
) -> None:
    _migration(
        versions, "0074", "0073", f'{CREATE_KIND}  # also mentions "gone" and "EXTRACT"'
    )
    blocking, covered = cover_with_pending_migrations(
        [discrepancy], applied_heads=("0073",), versions_dir=versions
    )
    assert blocking == [discrepancy]
    assert covered == []


def test_an_unknown_database_head_blocks_as_before(versions: Path) -> None:
    _migration(versions, "0074", "0073", CREATE_KIND)
    discrepancy = _missing("statement_run_kind_enum", "extract", "classify")
    for heads in (("unknown",), ()):
        blocking, covered = cover_with_pending_migrations(
            [discrepancy], applied_heads=heads, versions_dir=versions
        )
        assert blocking == [discrepancy]
        assert covered == []


def test_pending_revisions_follow_a_merge_revision(versions: Path) -> None:
    _migration(versions, "0074a", "0073")
    _migration(versions, "0074b", "0073")
    versions.joinpath("0075.py").write_text(
        'revision = "0075"\ndown_revision = ("0074a", "0074b")\n' + CREATE_KIND + "\n",
        encoding="utf-8",
    )
    blocking, covered = cover_with_pending_migrations(
        [_missing("statement_run_kind_enum", "extract", "classify")],
        applied_heads=("0074b",),
        versions_dir=versions,
    )
    assert blocking == []
    assert covered == [("statement_run_kind_enum", "0075")]


@pytest.fixture
def run_app(monkeypatch: pytest.MonkeyPatch, versions: Path) -> str:
    module = ModuleType("schema_gate_run_app")
    kind_type = SimpleNamespace(
        name="statement_run_kind_enum", enums=["extract", "classify"], native_enum=True
    )
    table = SimpleNamespace(columns=[SimpleNamespace(type=kind_type)])
    module.Base = SimpleNamespace(
        metadata=SimpleNamespace(tables={"statement_runs": table})
    )
    monkeypatch.setitem(sys.modules, "schema_gate_run_app", module)
    monkeypatch.setattr(
        gate,
        "ENUM_SOURCES",
        {
            "run/app": gate.EnumSource(
                metadata="schema_gate_run_app:Base.metadata",
                migrations_path=versions.name,
            )
        },
    )
    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        SimpleNamespace(connect=lambda _url: _Connection([])),
    )
    return "run/app"


class _Connection:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return self

    def execute(self, _query):
        return None

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_main_passes_when_a_pending_migration_creates_the_missing_type_and_reports_it(
    run_app: str,
    versions: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _migration(versions, "0074", "0073", CREATE_KIND)
    monkeypatch.setattr(gate, "fetch_applied_heads", lambda *_args: ("0073",))
    rc = gate.main(
        [
            "--service",
            run_app,
            "--db-url",
            "postgresql://db/app",
            "--app-path",
            str(versions.parent),
        ]
    )
    out = capsys.readouterr().out
    assert rc == gate.EXIT_OK
    assert "PENDING_MIGRATION_COVERS: statement_run_kind_enum (0074)" in out
    assert "ROLLBACK_CLASS: A" in out


def test_main_blocks_when_the_database_head_cannot_be_read(
    run_app: str,
    versions: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _migration(versions, "0074", "0073", CREATE_KIND)

    def unreadable(*_args):
        raise OSError("alembic_version is unreadable")

    monkeypatch.setattr(gate, "fetch_applied_heads", unreadable)
    rc = gate.main(
        [
            "--service",
            run_app,
            "--db-url",
            "postgresql://db/app",
            "--app-path",
            str(versions.parent),
        ]
    )
    assert rc == gate.EXIT_BLOCKED
    assert "statement_run_kind_enum" in capsys.readouterr().err


def test_main_blocks_when_a_revision_file_cannot_be_decoded(
    run_app: str, versions: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _migration(versions, "0074", "0073", CREATE_KIND)
    versions.joinpath("0075.py").write_bytes(b"\xff\xfe\x00")
    monkeypatch.setattr(gate, "fetch_applied_heads", lambda *_args: ("0073",))
    rc = gate.main(
        [
            "--service",
            run_app,
            "--db-url",
            "postgresql://db/app",
            "--app-path",
            str(versions.parent),
        ]
    )
    assert rc == gate.EXIT_BLOCKED


def test_main_keeps_blocking_a_service_that_names_no_migrations_path(
    run_app: str, versions: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source without `migrations_path` has the tolerance off, even when revision files sit in the app path."""
    _migration(versions, "0074", "0073", CREATE_KIND)
    monkeypatch.setattr(
        gate,
        "ENUM_SOURCES",
        {"run/app": gate.EnumSource(metadata="schema_gate_run_app:Base.metadata")},
    )
    monkeypatch.setattr(gate, "fetch_applied_heads", lambda *_args: ("0073",))
    rc = gate.main(
        [
            "--service",
            run_app,
            "--db-url",
            "postgresql://db/app",
            "--app-path",
            str(versions),
        ]
    )
    assert rc == gate.EXIT_BLOCKED
