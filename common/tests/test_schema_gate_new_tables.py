"""The pre-deploy schema gate tolerates an enum type that only new tables use (#1658).

Evidence: soak run 43360538840 of infra2 v3.1.58 blocked finance_report v0.2.65 because migration
0074 adds the table `statement_runs` with four enum columns. The gate compares the image's enum
types with the live database before the image's entrypoint runs `alembic upgrade head`, so the
types of a new table are always missing.

Two independent reviews of earlier versions showed that reading migration files (text, then ast)
cannot prove what `upgrade()` does. The rule here reads only facts: the ORM's table-to-type map
and the live database's table list.
"""

import sys
from types import ModuleType, SimpleNamespace

import pytest

from tools import pre_deploy_schema_check as gate
from tools.pre_deploy_schema_check import (
    EnumDiscrepancy,
    cover_with_new_tables,
    enum_tables_from_metadata,
)

KIND = "statement_run_kind_enum"
STATE = "statement_run_state_enum"


def _missing(name: str, *labels: str) -> EnumDiscrepancy:
    return EnumDiscrepancy(name, labels, (), labels, (), ())


def _cover(discrepancies, enum_tables, db_tables):
    return cover_with_new_tables(
        discrepancies, enum_tables=enum_tables, db_tables=db_tables
    )


def test_a_type_that_only_a_new_table_uses_is_covered() -> None:
    blocking, covered = _cover(
        [_missing(KIND, "extract", "classify")],
        {KIND: frozenset({"statement_runs"})},
        {"users", "statements"},
    )
    assert blocking == []
    assert covered == [(KIND, ("statement_runs",))]


def test_a_type_that_only_two_new_tables_use_is_covered() -> None:
    blocking, covered = _cover(
        [_missing(KIND, "extract")],
        {KIND: frozenset({"statement_runs", "statement_run_log"})},
        {"users"},
    )
    assert blocking == []
    assert covered == [(KIND, ("statement_run_log", "statement_runs"))]


def test_a_type_that_a_new_table_and_an_existing_table_use_blocks() -> None:
    discrepancy = _missing(KIND, "extract")
    tables = {KIND: frozenset({"statement_runs", "statements"})}
    assert _cover([discrepancy], tables, {"statements"}) == ([discrepancy], [])


def test_a_type_that_is_missing_while_its_table_exists_blocks() -> None:
    """Drift: the table is in the database, so no pending migration creates the type for it."""
    discrepancy = _missing(KIND, "extract")
    assert _cover(
        [discrepancy], {KIND: frozenset({"statement_runs"})}, {"statement_runs"}
    ) == (
        [discrepancy],
        [],
    )


def test_a_label_that_an_existing_type_lacks_blocks_even_for_a_new_table() -> None:
    discrepancy = EnumDiscrepancy(
        STATE, ("running", "parsing"), ("running",), ("parsing",), (), ()
    )
    assert _cover([discrepancy], {STATE: frozenset({"statement_runs"})}, {"users"}) == (
        [discrepancy],
        [],
    )


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
        EnumDiscrepancy("db_only_enum", (), ("a", "b"), (), ("a", "b"), ()),
    ],
)
def test_a_db_only_label_a_casing_drift_and_a_db_only_type_block(
    discrepancy: EnumDiscrepancy,
) -> None:
    tables = {KIND: frozenset({"statement_runs"})}
    assert _cover([discrepancy], tables, {"users"}) == ([discrepancy], [])


def test_a_missing_type_with_no_known_table_blocks() -> None:
    discrepancy = _missing("orphan_enum", "a")
    assert _cover([discrepancy], {}, {"users"}) == ([discrepancy], [])


def test_covered_and_blocking_discrepancies_are_split_in_one_call() -> None:
    new, drift = _missing(KIND, "extract"), _missing(STATE, "running")
    tables = {KIND: frozenset({"statement_runs"}), STATE: frozenset({"statements"})}
    assert _cover([new, drift], tables, {"statements"}) == (
        [drift],
        [(KIND, ("statement_runs",))],
    )


def _column(name: str, labels: tuple[str, ...]) -> SimpleNamespace:
    return SimpleNamespace(
        type=SimpleNamespace(name=name, enums=list(labels), native_enum=True)
    )


def test_enum_tables_map_each_type_to_the_tables_that_use_it() -> None:
    metadata = SimpleNamespace(
        tables={
            "statement_runs": SimpleNamespace(
                columns=[
                    _column(KIND, ("extract", "classify")),
                    SimpleNamespace(type=object()),
                ]
            ),
            "public.audit": SimpleNamespace(
                columns=[
                    _column(KIND, ("extract", "classify")),
                    _column(STATE, ("running",)),
                ]
            ),
            "plain": SimpleNamespace(columns=[SimpleNamespace(type=object())]),
        }
    )
    assert enum_tables_from_metadata(metadata) == {
        KIND: frozenset({"statement_runs", "audit"}),
        STATE: frozenset({"audit"}),
    }


def test_enum_tables_read_an_array_of_an_enum() -> None:
    array = SimpleNamespace(
        item_type=SimpleNamespace(name=KIND, enums=["extract"], native_enum=True)
    )
    metadata = SimpleNamespace(
        tables={"t": SimpleNamespace(columns=[SimpleNamespace(type=array)])}
    )
    assert enum_tables_from_metadata(metadata) == {KIND: frozenset({"t"})}


def test_enum_tables_reject_an_object_that_is_not_metadata() -> None:
    with pytest.raises(gate.GateNotEvaluated):
        enum_tables_from_metadata(SimpleNamespace())


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


@pytest.fixture
def run_app(monkeypatch: pytest.MonkeyPatch) -> str:
    module = ModuleType("schema_gate_run_app")
    table = SimpleNamespace(columns=[_column(KIND, ("extract", "classify"))])
    module.Base = SimpleNamespace(
        metadata=SimpleNamespace(tables={"statement_runs": table})
    )
    monkeypatch.setitem(sys.modules, "schema_gate_run_app", module)
    monkeypatch.setattr(
        gate,
        "ENUM_SOURCES",
        {"run/app": gate.EnumSource(metadata="schema_gate_run_app:Base.metadata")},
    )
    monkeypatch.setitem(
        sys.modules, "psycopg", SimpleNamespace(connect=lambda _url: _Connection([]))
    )
    return "run/app"


def _run_main(service: str) -> int:
    return gate.main(["--service", service, "--db-url", "postgresql://db/app"])


def test_main_passes_when_only_a_new_table_uses_the_missing_type_and_reports_it(
    run_app: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(gate, "fetch_db_tables", lambda _url: frozenset({"users"}))
    rc = _run_main(run_app)
    out = capsys.readouterr().out
    assert rc == gate.EXIT_OK
    assert f"NEW_TABLE_COVERS: {KIND} (statement_runs)" in out
    assert "1 covered by new tables" in out
    assert "ROLLBACK_CLASS: A" in out


def test_main_blocks_when_the_table_already_exists_in_the_database(
    run_app: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        gate, "fetch_db_tables", lambda _url: frozenset({"statement_runs"})
    )
    assert _run_main(run_app) == gate.EXIT_BLOCKED
    assert KIND in capsys.readouterr().err


def test_main_blocks_when_the_table_list_cannot_be_read(
    run_app: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unreadable(_url):
        raise OSError("information_schema is unreadable")

    monkeypatch.setattr(gate, "fetch_db_tables", unreadable)
    assert _run_main(run_app) == gate.EXIT_BLOCKED
    assert KIND in capsys.readouterr().err


def test_main_does_not_read_the_table_list_when_nothing_differs(
    run_app: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        SimpleNamespace(
            connect=lambda _url: _Connection([(KIND, "extract"), (KIND, "classify")])
        ),
    )

    calls: list[str] = []
    monkeypatch.setattr(
        gate, "fetch_db_tables", lambda url: calls.append(url) or frozenset()
    )
    assert _run_main(run_app) == gate.EXIT_OK
    assert calls == []


def test_fetch_db_tables_returns_the_table_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    class Recording(_Connection):
        def execute(self, query):
            seen.append(query)

    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        SimpleNamespace(connect=lambda _url: Recording([("users",), ("statements",)])),
    )
    assert gate.fetch_db_tables("postgresql://db/app") == frozenset(
        {"users", "statements"}
    )
    assert "information_schema.tables" in seen[0]
