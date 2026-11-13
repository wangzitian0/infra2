"""The pre-deploy schema gate tolerates enum DDL that a pending migration runs (#1658).

Evidence: soak run 43360538840 of infra2 v3.1.58 blocked finance_report v0.2.65 because migration
0074 creates four enum types. The gate compares the image's enum types with the live database
before the image's entrypoint runs `alembic upgrade head`, so a new type is always missing.

The first version matched type and label names as plain text anywhere in a migration file. An
independent review showed that a data backfill (`WHERE status = 'parsing'`), a comment and a
`downgrade()` body each satisfied it. These tests keep that review's inputs as regressions.
"""

import sys
import textwrap
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from tools import pre_deploy_schema_check as gate
from tools.pre_deploy_schema_check import EnumDiscrepancy, cover_with_pending_migrations

KIND = "statement_run_kind_enum"
ENUM_HEADER = f'from sqlalchemy.dialects import postgresql\nKIND = postgresql.ENUM("extract", "classify", name="{KIND}", create_type=False)\n'


def _write(
    versions: Path,
    revision: str,
    down: str | None,
    *,
    header: str = "",
    upgrade: str = "pass",
    downgrade: str = "pass",
) -> None:
    versions.joinpath(f"{revision}.py").write_text(
        f"{header}\nrevision: str = {revision!r}\ndown_revision: str | None = {down!r}\n\n"
        f"def upgrade():\n{textwrap.indent(upgrade, '    ')}\n\n"
        f"def downgrade():\n{textwrap.indent(downgrade, '    ')}\n",
        encoding="utf-8",
    )


def _missing(name: str, *labels: str, db: tuple[str, ...] = ()) -> EnumDiscrepancy:
    return EnumDiscrepancy(
        enum_name=name,
        code_values=db + labels,
        db_values=db,
        missing_in_db=labels,
        missing_in_code=(),
        casing_mismatches=(),
    )


def _cover(
    versions: Path, *discrepancies: EnumDiscrepancy, heads: tuple[str, ...] = ("0073",)
):
    return cover_with_pending_migrations(
        list(discrepancies), applied_heads=heads, versions_dir=versions
    )


@pytest.fixture
def versions(tmp_path: Path) -> Path:
    directory = tmp_path / "versions"
    directory.mkdir()
    _write(directory, "0073", None)
    return directory


def test_the_0074_pattern_that_defines_a_type_and_calls_create_is_covered(
    versions: Path,
) -> None:
    _write(
        versions,
        "0074",
        "0073",
        header=ENUM_HEADER,
        upgrade="for t in (KIND,):\n    t.create(bind, checkfirst=True)",
    )
    blocking, covered = _cover(versions, _missing(KIND, "extract", "classify"))
    assert blocking == []
    assert covered == [(KIND, "0074")]


def test_a_type_created_by_an_enum_call_in_the_upgrade_body_is_covered(
    versions: Path,
) -> None:
    body = f'op.create_table("t", sa.Column("k", postgresql.ENUM("extract", "classify", name="{KIND}")))'
    _write(versions, "0074", "0073", upgrade=body)
    blocking, covered = _cover(versions, _missing(KIND, "extract", "classify"))
    assert blocking == []
    assert covered == [(KIND, "0074")]


def test_an_enum_call_with_create_type_false_and_no_create_call_blocks(
    versions: Path,
) -> None:
    """The repo's normal pattern for a column of an existing type: it creates nothing."""
    _write(
        versions,
        "0074",
        "0073",
        header=ENUM_HEADER,
        upgrade='op.add_column("t", sa.Column("k", KIND))',
    )
    discrepancy = _missing(KIND, "extract", "classify")
    assert _cover(versions, discrepancy) == ([discrepancy], [])


def test_a_comment_that_names_the_type_and_labels_blocks(versions: Path) -> None:
    _write(
        versions,
        "0074",
        "0073",
        header=f'# TODO: create ENUM("extract", "classify", name="{KIND}")\n',
        upgrade="pass  # add 'archived' to account_status_enum",
    )
    created = _missing(KIND, "extract", "classify")
    added = _missing("account_status_enum", "archived", db=("open",))
    assert _cover(versions, created, added) == ([created, added], [])


def test_a_downgrade_body_does_not_cover_anything(versions: Path) -> None:
    _write(
        versions,
        "0074",
        "0073",
        downgrade=(
            "op.execute(\"ALTER TYPE account_status_enum ADD VALUE 'archived'\")\n"
            f'postgresql.ENUM("extract", "classify", name="{KIND}").create(bind)'
        ),
    )
    created = _missing(KIND, "extract", "classify")
    added = _missing("account_status_enum", "archived", db=("open",))
    assert _cover(versions, created, added) == ([created, added], [])


def test_a_label_that_a_data_backfill_quotes_does_not_cover_a_new_label(
    versions: Path,
) -> None:
    """Review input: the real 0074 defines the state type with create_type=False and quotes 'parsing' in a backfill."""
    header = 'STATE = postgresql.ENUM("requested", "running", name="statement_run_state_enum", create_type=False)\n'
    _write(
        versions,
        "0074",
        "0073",
        header=header,
        upgrade="op.execute(\"UPDATE t SET k = 'x' WHERE s.status = 'parsing'\")",
    )
    discrepancy = _missing(
        "statement_run_state_enum", "parsing", db=("requested", "running")
    )
    assert _cover(versions, discrepancy) == ([discrepancy], [])


@pytest.mark.parametrize("clause", ["ADD VALUE", "ADD VALUE IF NOT EXISTS"])
def test_a_label_added_by_alter_type_add_value_is_covered(
    versions: Path, clause: str
) -> None:
    _write(
        versions,
        "0074",
        "0073",
        upgrade=f"op.execute(\"ALTER TYPE {KIND} {clause} 'backfill'\")",
    )
    blocking, covered = _cover(
        versions, _missing(KIND, "backfill", db=("extract", "classify"))
    )
    assert blocking == []
    assert covered == [(KIND, "0074")]


def test_a_type_that_a_later_pending_revision_drops_is_not_covered(
    versions: Path,
) -> None:
    _write(versions, "0074", "0073", header=ENUM_HEADER, upgrade="KIND.create(bind)")
    _write(
        versions, "0075", "0074", upgrade=f'op.execute("DROP TYPE IF EXISTS {KIND}")'
    )
    discrepancy = _missing(KIND, "extract", "classify")
    assert _cover(versions, discrepancy) == ([discrepancy], [])


def test_dropping_a_type_with_a_longer_name_does_not_count_as_a_drop(
    versions: Path,
) -> None:
    _write(versions, "0074", "0073", header=ENUM_HEADER, upgrade="KIND.create(bind)")
    _write(versions, "0075", "0074", upgrade=f'op.execute("DROP TYPE {KIND}_v2")')
    blocking, covered = _cover(versions, _missing(KIND, "extract", "classify"))
    assert blocking == []
    assert covered == [(KIND, "0074")]


def test_a_type_created_in_one_pending_revision_and_extended_in_the_next_is_covered(
    versions: Path,
) -> None:
    _write(versions, "0074", "0073", header=ENUM_HEADER, upgrade="KIND.create(bind)")
    _write(
        versions,
        "0075",
        "0074",
        upgrade=f"op.execute(\"ALTER TYPE {KIND} ADD VALUE 'backfill'\")",
    )
    blocking, covered = _cover(
        versions, _missing(KIND, "extract", "classify", "backfill")
    )
    assert blocking == []
    assert covered == [(KIND, "0074,0075")]


def test_a_type_whose_name_only_extends_the_missing_one_does_not_cover_it(
    versions: Path,
) -> None:
    header = ENUM_HEADER.replace(f'name="{KIND}"', f'name="{KIND}_v2"')
    _write(versions, "0074", "0073", header=header, upgrade="KIND.create(bind)")
    discrepancy = _missing(KIND, "extract", "classify")
    assert _cover(versions, discrepancy) == ([discrepancy], [])


def test_a_label_that_only_extends_the_missing_one_does_not_cover_it(
    versions: Path,
) -> None:
    header = ENUM_HEADER.replace('"extract"', '"extract_x"')
    _write(versions, "0074", "0073", header=header, upgrade="KIND.create(bind)")
    discrepancy = _missing(KIND, "extract", "classify")
    assert _cover(versions, discrepancy) == ([discrepancy], [])


def test_a_migration_that_is_already_applied_does_not_cover_a_missing_type(
    versions: Path,
) -> None:
    """The database ran 0074 and still lacks the type: that is a defect, not a pending change."""
    _write(versions, "0074", "0073", header=ENUM_HEADER, upgrade="KIND.create(bind)")
    discrepancy = _missing(KIND, "extract", "classify")
    assert _cover(versions, discrepancy, heads=("0074",)) == ([discrepancy], [])


@pytest.mark.parametrize(
    "discrepancy",
    [
        EnumDiscrepancy(KIND, ("extract",), ("extract", "gone"), (), ("gone",), ()),
        EnumDiscrepancy(
            KIND,
            ("EXTRACT",),
            ("extract",),
            ("EXTRACT",),
            (),
            (("EXTRACT", "extract"),),
        ),
        EnumDiscrepancy(
            KIND, (), ("extract", "classify"), (), ("extract", "classify"), ()
        ),
        EnumDiscrepancy(
            KIND, ("extract", "new"), ("extract", "old"), ("new",), ("old",), ()
        ),
    ],
)
def test_a_db_only_label_a_casing_drift_and_a_rename_still_block(
    versions: Path, discrepancy: EnumDiscrepancy
) -> None:
    alters = "\n".join(
        f"op.execute(\"ALTER TYPE {KIND} ADD VALUE '{label}'\")"
        for label in ("new", "EXTRACT")
    )
    _write(versions, "0074", "0073", upgrade=alters)
    assert _cover(versions, discrepancy) == ([discrepancy], [])


def test_one_unknown_database_head_blocks_even_when_another_head_is_known(
    versions: Path,
) -> None:
    _write(versions, "0074", "0073", header=ENUM_HEADER, upgrade="KIND.create(bind)")
    discrepancy = _missing(KIND, "extract", "classify")
    for heads in (("0073", "unknown"), ("unknown",), ()):
        assert _cover(versions, discrepancy, heads=heads) == ([discrepancy], [])


def test_pending_revisions_follow_a_merge_whose_parents_span_several_lines(
    versions: Path,
) -> None:
    _write(versions, "0074a", "0073")
    _write(versions, "0074b", "0073")
    versions.joinpath("0075.py").write_text(
        'revision = "0075"\ndown_revision = (\n    "0074a",\n    "0074b",\n)\n\n'
        + ENUM_HEADER
        + "\ndef upgrade():\n    KIND.create(bind)\n",
        encoding="utf-8",
    )
    blocking, covered = _cover(
        versions, _missing(KIND, "extract", "classify"), heads=("0074b",)
    )
    assert blocking == []
    assert covered == [(KIND, "0075")]


def test_an_enum_call_that_redeclares_an_existing_type_does_not_add_a_label(
    versions: Path,
) -> None:
    """`ENUM(...).create(checkfirst=True)` skips a type that exists, so its labels are not added."""
    _write(
        versions,
        "0074",
        "0073",
        header=ENUM_HEADER,
        upgrade="KIND.create(bind, checkfirst=True)",
    )
    discrepancy = _missing(KIND, "classify", db=("extract",))
    assert _cover(versions, discrepancy) == ([discrepancy], [])


@pytest.fixture
def run_app(monkeypatch: pytest.MonkeyPatch, versions: Path) -> str:
    module = ModuleType("schema_gate_run_app")
    kind_type = SimpleNamespace(
        name=KIND, enums=["extract", "classify"], native_enum=True
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
        sys.modules, "psycopg", SimpleNamespace(connect=lambda _url: _Connection([]))
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


def _run_main(service: str, versions: Path) -> int:
    return gate.main(
        [
            "--service",
            service,
            "--db-url",
            "postgresql://db/app",
            "--app-path",
            str(versions.parent),
        ]
    )


def test_main_passes_when_a_pending_migration_creates_the_missing_type_and_reports_it(
    run_app: str,
    versions: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write(versions, "0074", "0073", header=ENUM_HEADER, upgrade="KIND.create(bind)")
    monkeypatch.setattr(gate, "fetch_applied_heads", lambda *_args: ("0073",))
    rc = _run_main(run_app, versions)
    out = capsys.readouterr().out
    assert rc == gate.EXIT_OK
    assert f"PENDING_MIGRATION_COVERS: {KIND} (0074)" in out
    assert "ROLLBACK_CLASS: A" in out


def test_main_blocks_when_the_database_head_cannot_be_read(
    run_app: str,
    versions: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write(versions, "0074", "0073", header=ENUM_HEADER, upgrade="KIND.create(bind)")

    def unreadable(*_args):
        raise OSError("alembic_version is unreadable")

    monkeypatch.setattr(gate, "fetch_applied_heads", unreadable)
    assert _run_main(run_app, versions) == gate.EXIT_BLOCKED
    assert KIND in capsys.readouterr().err


@pytest.mark.parametrize("content", [b"\xff\xfe\x00", b"def upgrade(:\n"])
def test_main_blocks_when_a_revision_file_cannot_be_read_as_python(
    run_app: str, versions: Path, monkeypatch: pytest.MonkeyPatch, content: bytes
) -> None:
    _write(versions, "0074", "0073", header=ENUM_HEADER, upgrade="KIND.create(bind)")
    versions.joinpath("0075.py").write_bytes(content)
    monkeypatch.setattr(gate, "fetch_applied_heads", lambda *_args: ("0073",))
    assert _run_main(run_app, versions) == gate.EXIT_BLOCKED


def test_main_keeps_blocking_a_service_that_names_no_migrations_path(
    run_app: str, versions: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source without `migrations_path` has the tolerance off, even when revision files sit in the app path."""
    _write(versions, "0074", "0073", header=ENUM_HEADER, upgrade="KIND.create(bind)")
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
