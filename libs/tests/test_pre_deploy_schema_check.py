"""Tests for pre-deploy schema & enum check gate (#698)."""

import sys
import textwrap
from enum import Enum
from types import ModuleType, SimpleNamespace

import pytest

from tools import pre_deploy_schema_check as gate
from tools.pre_deploy_schema_check import (
    compare_enum_values,
    extract_enum_values,
    check_enums,
    query_db_enums,
)


class StatusEnum(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


def test_compare_enum_values_exact_match():
    code_vals = ["PENDING", "PROCESSING", "COMPLETED"]
    db_vals = ["PENDING", "PROCESSING", "COMPLETED"]
    disc = compare_enum_values("status", code_vals, db_vals)
    assert not disc.has_error
    assert disc.missing_in_db == ()
    assert disc.casing_mismatches == ()


def test_compare_enum_values_casing_divergence():
    # Replays #698 incident: code uses uppercase, DB uses lowercase
    code_vals = ["PENDING", "PROCESSING", "COMPLETED"]
    db_vals = ["pending", "processing", "completed"]
    disc = compare_enum_values("status", code_vals, db_vals)
    assert disc.has_error
    assert set(disc.casing_mismatches) == {
        ("PENDING", "pending"),
        ("PROCESSING", "processing"),
        ("COMPLETED", "completed"),
    }


def test_compare_enum_values_missing_in_db():
    code_vals = ["PENDING", "PROCESSING", "ARCHIVED"]
    db_vals = ["PENDING", "PROCESSING"]
    disc = compare_enum_values("status", code_vals, db_vals)
    assert disc.has_error
    assert disc.missing_in_db == ("ARCHIVED",)


def test_extract_enum_values():
    vals = extract_enum_values(StatusEnum)
    assert vals == ["PENDING", "PROCESSING", "COMPLETED", "FAILED"]


def test_check_enums_blocks_on_discrepancy():
    code_enums = {
        "status": StatusEnum,
    }
    db_enums = {
        "status": [
            "pending",
            "processing",
            "completed",
            "failed",
        ],  # lowercase mismatch!
    }
    discrepancies = check_enums(code_enums, db_enums)
    assert len(discrepancies) == 1
    assert discrepancies[0].has_error
    assert len(discrepancies[0].casing_mismatches) == 4


def test_query_db_enums_from_cursor():
    class FakeCursor:
        def fetchall(self):
            return [
                ("report_status", "draft"),
                ("report_status", "published"),
                ("user_role", "admin"),
                ("user_role", "viewer"),
            ]

    enums = query_db_enums(FakeCursor())
    assert enums["report_status"] == ["draft", "published"]
    assert enums["user_role"] == ["admin", "viewer"]


# --- Audit-hardened tests (反事实审计 2026-09-16) ---


def test_missing_in_code_is_error():
    """DB superset is dangerous: values code doesn't know → runtime deserialization failure."""
    code_vals = ["PENDING", "PROCESSING"]
    db_vals = ["PENDING", "PROCESSING", "ARCHIVED"]  # DB has more
    disc = compare_enum_values("status", code_vals, db_vals)
    assert disc.has_error  # was False before fix!
    assert disc.missing_in_code == ("ARCHIVED",)


def test_bidirectional_strict_equality():
    """Both directions must match for no error."""
    code_vals = ["A", "B", "C"]
    db_vals = ["A", "B", "C"]
    disc = compare_enum_values("status", code_vals, db_vals)
    assert not disc.has_error

    # Asymmetry in either direction is an error
    disc2 = compare_enum_values("status", ["A", "B"], ["A", "B", "C"])
    assert disc2.has_error  # missing_in_code
    disc3 = compare_enum_values("status", ["A", "B", "C"], ["A", "B"])
    assert disc3.has_error  # missing_in_db


def test_db_only_enum_detected():
    """DB enum type exists but has no code counterpart at all."""
    code_enums = {"status": ["PENDING", "DONE"]}
    db_enums = {
        "status": ["PENDING", "DONE"],
        "priority": ["low", "high"],  # DB-only, no code declaration
    }
    discrepancies = check_enums(code_enums, db_enums)
    assert len(discrepancies) == 1
    assert discrepancies[0].enum_name == "priority"
    assert discrepancies[0].has_error
    assert discrepancies[0].missing_in_code == ("low", "high")


def test_check_enums_bidirectional_with_enum_class():
    """Full integration: Enum class vs DB with superset."""
    code_enums = {"status": StatusEnum}
    db_enums = {
        "status": ["PENDING", "PROCESSING", "COMPLETED", "FAILED", "CANCELLED"],
    }
    discrepancies = check_enums(code_enums, db_enums)
    assert len(discrepancies) == 1
    assert discrepancies[0].missing_in_code == ("CANCELLED",)
    assert discrepancies[0].has_error


# --- Fail-closed on missing inputs (#718 review threads) ---


def _enum_column(name, labels, *, native=True):
    return SimpleNamespace(
        type=SimpleNamespace(name=name, enums=list(labels), native_enum=native)
    )


def _metadata(*tables):
    return SimpleNamespace(
        tables={
            f"t{index}": SimpleNamespace(columns=list(columns))
            for index, columns in enumerate(tables)
        }
    )


FR_LIKE_METADATA = _metadata(
    [
        SimpleNamespace(type=SimpleNamespace()),  # a plain column: no enum
        _enum_column("account_type_enum", ["ASSET", "LIABILITY"]),
    ],
    [
        _enum_column("journal_entry_status_enum", ["draft", "posted"]),
        # the same type on a second column is one enum, not a conflict
        _enum_column("account_type_enum", ["LIABILITY", "ASSET"]),
        # native_enum=False is a VARCHAR + CHECK: no Postgres type to compare
        _enum_column("inline_check", ["a", "b"], native=False),
        SimpleNamespace(
            type=SimpleNamespace(
                item_type=SimpleNamespace(name="tag_enum", enums=["x"])
            )
        ),
    ],
)


@pytest.fixture
def fake_app(monkeypatch):
    """A registered service whose metadata module is importable."""
    module = ModuleType("schema_gate_fake_app")
    module.Base = SimpleNamespace(metadata=FR_LIKE_METADATA)
    monkeypatch.setitem(sys.modules, "schema_gate_fake_app", module)
    monkeypatch.setattr(
        gate,
        "ENUM_SOURCES",
        {"fake/app": gate.EnumSource(metadata="schema_gate_fake_app:Base.metadata")},
    )
    return "fake/app"


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, _query):
        return None

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _fake_psycopg(monkeypatch, rows, seen_urls):
    class _Conn:
        def cursor(self):
            return _FakeCursor(rows)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def connect(url):
        seen_urls.append(url)
        return _Conn()

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))


def test_metadata_enums_are_keyed_by_postgres_type_name() -> None:
    enums = gate.enums_from_metadata(FR_LIKE_METADATA)
    assert enums == {
        "account_type_enum": ("ASSET", "LIABILITY"),
        "journal_entry_status_enum": ("draft", "posted"),
        "tag_enum": ("x",),
    }


def test_conflicting_labels_for_one_enum_type_are_not_evaluated() -> None:
    metadata = _metadata(
        [_enum_column("status_enum", ["a", "b"])],
        [_enum_column("status_enum", ["a", "c"])],
    )
    with pytest.raises(gate.GateNotEvaluated, match="different labels"):
        gate.enums_from_metadata(metadata)


def test_the_module_path_from_718_does_not_load_and_is_not_evaluated() -> None:
    """Replays the review finding: finance_report.models.enums does not exist."""
    sources = {
        "finance_report/app": gate.EnumSource(
            metadata="finance_report.models.enums:Base.metadata"
        )
    }
    with pytest.raises(gate.GateNotEvaluated, match="cannot load finance_report/app"):
        gate.load_code_enums_for_service("finance_report/app", sources=sources)


def test_registered_finance_report_source_names_the_app_package() -> None:
    source = gate.ENUM_SOURCES["finance_report/app"]
    assert source.metadata == "src.database:Base.metadata"
    assert source.imports == ("src.orm_registry",)
    assert "apps/backend" in source.app_path


def test_unregistered_service_is_not_evaluated() -> None:
    with pytest.raises(gate.GateNotEvaluated, match="no enum source is registered"):
        gate.load_code_enums_for_service("platform/redis")


def test_metadata_without_native_enums_is_not_evaluated(monkeypatch) -> None:
    module = ModuleType("schema_gate_empty_app")
    module.metadata = _metadata([SimpleNamespace(type=SimpleNamespace())])
    monkeypatch.setitem(sys.modules, "schema_gate_empty_app", module)
    sources = {"empty/app": gate.EnumSource(metadata="schema_gate_empty_app:metadata")}
    with pytest.raises(gate.GateNotEvaluated, match="no native enum types"):
        gate.load_code_enums_for_service("empty/app", sources=sources)


def test_app_path_makes_the_service_package_importable(tmp_path, monkeypatch) -> None:
    package = tmp_path / "schema_gate_pkg_app"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "registry.py").write_text(
        textwrap.dedent(
            """
            from types import SimpleNamespace
            from schema_gate_pkg_app import database
            database.Base.metadata.tables["loaded"] = SimpleNamespace(columns=[
                SimpleNamespace(type=SimpleNamespace(name="role_enum", enums=["admin"]))
            ])
            """
        ),
        encoding="utf-8",
    )
    (package / "database.py").write_text(
        textwrap.dedent(
            """
            from types import SimpleNamespace
            Base = SimpleNamespace(metadata=SimpleNamespace(tables={}))
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "path", list(sys.path))
    for name in (
        "schema_gate_pkg_app",
        "schema_gate_pkg_app.database",
        "schema_gate_pkg_app.registry",
    ):
        monkeypatch.delitem(sys.modules, name, raising=False)
    sources = {
        "pkg/app": gate.EnumSource(
            metadata="schema_gate_pkg_app.database:Base.metadata",
            imports=("schema_gate_pkg_app.registry",),
        )
    }
    assert gate.load_code_enums_for_service(
        "pkg/app", app_path=str(tmp_path), sources=sources
    ) == {"role_enum": ("admin",)}


def test_main_without_database_url_is_not_evaluated(
    fake_app, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert gate.main(["--service", fake_app]) == gate.EXIT_NOT_EVALUATED
    err = capsys.readouterr().err
    assert "NOT EVALUATED" in err
    assert "no database URL" in err


def test_main_with_unloadable_enums_never_touches_the_database(
    monkeypatch, capsys
) -> None:
    seen: list[str] = []
    _fake_psycopg(monkeypatch, [], seen)
    rc = gate.main(["--service", "platform/redis", "--db-url", "postgresql://db/x"])
    assert rc == gate.EXIT_NOT_EVALUATED
    assert seen == []
    assert "no enum source is registered" in capsys.readouterr().err


def test_main_reports_every_missing_input(monkeypatch, capsys) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert gate.main(["--service", "platform/redis"]) == gate.EXIT_NOT_EVALUATED
    err = capsys.readouterr().err
    assert "no database URL" in err
    assert "no enum source is registered" in err


def test_main_passes_only_when_both_sides_match(fake_app, monkeypatch, capsys) -> None:
    seen: list[str] = []
    _fake_psycopg(
        monkeypatch,
        [
            ("account_type_enum", "ASSET"),
            ("account_type_enum", "LIABILITY"),
            ("journal_entry_status_enum", "draft"),
            ("journal_entry_status_enum", "posted"),
            ("tag_enum", "x"),
        ],
        seen,
    )
    rc = gate.main(
        ["--service", fake_app, "--db-url", "postgresql+asyncpg://u:p@db:5432/app"]
    )
    assert rc == gate.EXIT_OK
    assert seen == ["postgresql://u:p@db:5432/app"]  # libpq can't take +asyncpg
    assert "3 enum types, 0 discrepancies" in capsys.readouterr().out


def test_main_blocks_on_the_698_casing_divergence(
    fake_app, monkeypatch, capsys
) -> None:
    _fake_psycopg(
        monkeypatch,
        [
            ("account_type_enum", "asset"),
            ("account_type_enum", "liability"),
            ("journal_entry_status_enum", "draft"),
            ("journal_entry_status_enum", "posted"),
            ("tag_enum", "x"),
        ],
        [],
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://db/app")
    assert gate.main(["--service", fake_app]) == gate.EXIT_BLOCKED
    assert "account_type_enum" in capsys.readouterr().err


def test_main_blocks_when_the_database_is_unreachable(fake_app, monkeypatch) -> None:
    def connect(_url):
        raise OSError("connection refused")

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    assert gate.main(["--service", fake_app, "--db-url", "postgresql://db/app"]) == (
        gate.EXIT_BLOCKED
    )


def test_query_db_enums_rejects_an_object_that_cannot_query() -> None:
    with pytest.raises(TypeError):
        query_db_enums(object())


def test_libpq_url_keeps_plain_urls() -> None:
    assert gate.libpq_url("postgresql://h/db") == "postgresql://h/db"
    assert gate.libpq_url("postgres+psycopg2://h/db") == "postgres://h/db"
