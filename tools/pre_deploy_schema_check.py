"""Pre-deploy schema & enum consistency check gate (#698).

Validates that application Python ORM / data model Enum definitions **strictly match**
the database enum types before a deploy destroys or restarts running containers.
During the 2026-09-10 incident (#698), an enum case mismatch ('PENDING' vs 'pending')
caused finance_report backend to fail during startup, bringing the service down for
13 minutes because containers were removed before verifying schema compatibility.

Audit-hardened (反事实审计 2026-09-16):
- Bidirectional comparison: missing_in_db AND missing_in_code both block deploy.
  DB superset is the most dangerous runtime divergence. One exception (#1658): a
  missing_in_db discrepancy that a migration newer than the database's alembic head
  fixes in its ``upgrade()`` statements (``ENUM(..., name=<type>)`` that creates the type,
  or ``ALTER TYPE <type> ADD VALUE '<label>'``) does not block. See
  ``cover_with_pending_migrations``.
- Fail-closed: any exception (query failure, parse error) blocks deploy.
  A check that silently fails is worse than no check.
- Never a silent pass (#718 review): a gate that cannot load its code-side enums or
  has no database to compare against has not evaluated anything, so it exits
  ``EXIT_NOT_EVALUATED`` — non-zero, like a finding.

Exit codes: 0 verified (0 discrepancies, or only covered ones) · 1 blocked (discrepancy or DB failure) ·
3 not evaluated (an input is unavailable) — also blocking.

The code side is the service's own SQLAlchemy metadata, so the gate runs where the
application imports (its image or virtualenv), not from infra2's environment::

    python tools/pre_deploy_schema_check.py --service finance_report/app
        --app-path <finance_report checkout>/apps/backend --db-url postgresql://...
"""

from __future__ import annotations

import argparse
import ast
import importlib
import os
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

EXIT_OK = 0
EXIT_BLOCKED = 1
# 2 is argparse's usage error; "not evaluated" gets its own code so a caller can tell
# "the schemas differ" from "nothing was compared" — both still block.
EXIT_NOT_EVALUATED = 3


class GateNotEvaluated(Exception):
    """An input the gate needs is unavailable, so there is no verdict — which blocks."""


@dataclass(frozen=True)
class EnumSource:
    """Where a service's code-side enum truth and declarative data preconditions live.

    ``metadata`` names a SQLAlchemy ``MetaData`` as ``module:attribute.path``; every
    native enum column type registered on it is one Postgres enum type (its ``name``)
    with its labels (``enums``). ``imports`` are imported first, for their side effect
    of registering every mapped class on that metadata. ``app_path`` says which
    directory of the service's checkout has to be on ``sys.path``.
    ``requires_migration`` sets an optional minimum migration version floor checked
    against ``migration_table`` (default ``alembic_version.version_num``) (#609).
    ``preconditions`` declares named read-only SQL predicates ``(name, sql, remedy)``
    that must all hold before any container is replaced (#609).
    ``migrations_path`` (relative to ``--app-path`` or the working directory) holds the
    alembic revision files; empty turns the pending-migration tolerance off (#1658).
    """

    metadata: str
    imports: tuple[str, ...] = ()
    app_path: str = ""
    requires_migration: str | None = None
    migration_table: str = "alembic_version"
    migration_column: str = "version_num"
    preconditions: tuple[tuple[str, str, str], ...] = ()
    migrations_path: str = ""


# finance_report's backend is the ``src`` package under apps/backend of the
# finance_report repository — there is no ``finance_report.models.enums`` (the infra2
# ``finance_report`` package is deploy config). Its mapped classes register on
# ``src.database.Base.metadata`` once ``src.orm_registry`` is imported, and every
# Postgres enum there carries an explicit ``name=`` (``account_type_enum``, migration
# 0007) that no class-name derivation reproduces.
ENUM_SOURCES: dict[str, EnumSource] = {
    "finance_report/app": EnumSource(
        metadata="src.database:Base.metadata",
        imports=("src.orm_registry",),
        app_path="<finance_report checkout>/apps/backend",
        migrations_path="migrations/versions",
    ),
}


@dataclass(frozen=True)
class EnumDiscrepancy:
    enum_name: str
    code_values: tuple[str, ...]
    db_values: tuple[str, ...]
    missing_in_db: tuple[str, ...]
    missing_in_code: tuple[str, ...]
    casing_mismatches: tuple[tuple[str, str], ...]

    @property
    def has_error(self) -> bool:
        """Bidirectional: any direction of mismatch is an error.

        - missing_in_db: code has values DB doesn't → deploy will INSERT unknown enum → crash
        - missing_in_code: DB has values code doesn't know → runtime deserialization failures
        - casing_mismatches: same logical value, different case → silent data corruption
        """
        return bool(
            self.missing_in_db or self.missing_in_code or self.casing_mismatches
        )


# Infra-022 T3.2 / TODOWRITE:20: the safety class an automatic-rollback circuit breaker
# would need before this deploy's schema state could be reversed. Only Class A may ever
# auto-rollback; Class C never may (DROP/RENAME/tightened constraints require a human
# forward-fix, production_resilience_and_dr.md T3.2). This module has no visibility into
# raw migration DDL — it can only classify from the SAME bidirectional enum comparison
# the gate already computes, so it distinguishes exactly two tiers, not three: A
# (nothing destructive observed) and C (something was).
#
# Both ``missing_in_code`` and ``casing_mismatches`` are treated as Class C — this is a
# DELIBERATE choice below (``or``), not a derived necessity: they are NOT always
# coupled. ``compare_enum_values`` computes them from independent comparisons (a plain
# set difference for missing_in_code; a separate case-insensitive scan for
# casing_mismatches), so casing drift CAN occur with an empty ``missing_in_code`` — e.g.
# code declaring both case variants (``["PENDING", "pending"]``) against a DB with only
# ``["pending"]`` sets ``casing_mismatches=(("PENDING","pending"),)`` while
# ``missing_in_code=()`` (the lowercase member alone already satisfies the DB side).
# Class C classifies it anyway because same-value casing drift is "silent data
# corruption" per ``EnumDiscrepancy.has_error`` — never treated as the safe tier.
ROLLBACK_CLASS_A = "A"  # no destructive signal observed -- automatic rollback permitted
ROLLBACK_CLASS_C = "C"  # DB carries structure the code no longer declares -- never auto


def classify_rollback(discrepancies: Sequence[EnumDiscrepancy]) -> str:
    """ROLLBACK_CLASS for this comparison (Infra-022 T3.2's rollback safety class).

    - Class C: any discrepancy has ``missing_in_code`` (the DB carries an enum label, or
      a whole enum type, the code no longer declares) OR a same-value casing drift —
      independent signals, both treated as Class C (see the module-level comment above
      for why they are not always coupled). ``missing_in_code`` is the direction that
      corresponds to a DROP/RENAME having already reached the database; the old code
      cannot be safely restored against it. This also covers a DB-only enum type
      (``code_values=()``).
    - Class A: zero discrepancies, or only ``missing_in_db`` (code is ahead of a
      not-yet-migrated DB — purely additive; old code neither reads nor needs the new
      label).
    """
    if any(disc.missing_in_code or disc.casing_mismatches for disc in discrepancies):
        return ROLLBACK_CLASS_C
    return ROLLBACK_CLASS_A


def compare_enum_values(
    enum_name: str, code_values: Sequence[str], db_values: Sequence[str]
) -> EnumDiscrepancy:
    """Compare code enum values against database enum values for exact match and case divergence."""
    code_set = set(code_values)
    db_set = set(db_values)

    missing_in_db = tuple(sorted(code_set - db_set))
    missing_in_code = tuple(sorted(db_set - code_set))

    # Detect casing divergence: e.g. code has 'PENDING' while DB has 'pending'
    casing_mismatches: list[tuple[str, str]] = []
    db_lower_map = {v.lower(): v for v in db_values}
    for cv in code_values:
        if cv not in db_set and cv.lower() in db_lower_map:
            casing_mismatches.append((cv, db_lower_map[cv.lower()]))

    return EnumDiscrepancy(
        enum_name=enum_name,
        code_values=tuple(code_values),
        db_values=tuple(db_values),
        missing_in_db=missing_in_db,
        missing_in_code=missing_in_code,
        casing_mismatches=tuple(casing_mismatches),
    )


def extract_enum_values(enum_cls: type[Enum]) -> list[str]:
    """Extract string values from a Python Enum class."""
    values = []
    for member in enum_cls:
        if isinstance(member.value, str):
            values.append(member.value)
        else:
            values.append(str(member.value))
    return values


def query_db_enums(connection_or_cursor: Any) -> dict[str, list[str]]:
    """Query PostgreSQL pg_type and pg_enum to get all defined database enums and their labels."""
    query = """
    SELECT
        t.typname AS enum_name,
        e.enumlabel AS enum_value
    FROM pg_type t
    JOIN pg_enum e ON t.oid = e.enumtypid
    JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
    ORDER BY t.typname, e.enumsortorder;
    """
    if hasattr(connection_or_cursor, "execute"):
        cursor = (
            connection_or_cursor.cursor()
            if hasattr(connection_or_cursor, "cursor")
            else connection_or_cursor
        )
        cursor.execute(query)
        rows = cursor.fetchall()
    elif hasattr(connection_or_cursor, "fetchall"):
        rows = connection_or_cursor.fetchall()
    else:
        # An object that can neither run the query nor hand back rows must not read as
        # "the database has no enums".
        raise TypeError(
            f"cannot query enums through {type(connection_or_cursor).__name__}"
        )

    enums: dict[str, list[str]] = {}
    for row in rows:
        name = row[0] if isinstance(row, (tuple, list)) else row.get("enum_name")
        val = row[1] if isinstance(row, (tuple, list)) else row.get("enum_value")
        enums.setdefault(name, []).append(val)
    return enums


def check_enums(
    code_enums: dict[str, type[Enum] | Sequence[str]],
    db_enums: dict[str, Sequence[str]],
) -> list[EnumDiscrepancy]:
    """Check a mapping of code enums against database enums.

    Bidirectional: checks both directions (code→DB and DB→code).
    Also detects DB enums that exist but have no code counterpart.
    """
    discrepancies: list[EnumDiscrepancy] = []
    for name, code_def in code_enums.items():
        if isinstance(code_def, type) and issubclass(code_def, Enum):
            code_vals = extract_enum_values(code_def)
        else:
            code_vals = list(code_def)

        db_vals = db_enums.get(name, [])
        if not db_vals:
            # DB enum missing entirely
            discrepancies.append(
                EnumDiscrepancy(
                    enum_name=name,
                    code_values=tuple(code_vals),
                    db_values=(),
                    missing_in_db=tuple(code_vals),
                    missing_in_code=(),
                    casing_mismatches=(),
                )
            )
            continue

        disc = compare_enum_values(name, code_vals, db_vals)
        if disc.has_error:
            discrepancies.append(disc)

    # Reverse check: DB enums not declared in code at all
    for db_name in db_enums:
        if db_name not in code_enums:
            discrepancies.append(
                EnumDiscrepancy(
                    enum_name=db_name,
                    code_values=(),
                    db_values=tuple(db_enums[db_name]),
                    missing_in_db=(),
                    missing_in_code=tuple(db_enums[db_name]),
                    casing_mismatches=(),
                )
            )

    return discrepancies


@dataclass(frozen=True)
class _Revision:
    parents: tuple[str, ...]
    upgrade: ast.FunctionDef | None
    module: ast.Module


@dataclass
class _EnumEvidence:
    """What the pending revisions do to one enum type (#1658)."""

    created: set[str] = field(default_factory=set)
    added: set[str] = field(default_factory=set)
    creates: bool = False
    dropped: bool = False
    revisions: set[str] = field(default_factory=set)


def _assigned_literal(module: ast.Module, name: str) -> Any:
    """The literal value of a top-level ``name = ...`` or ``name: T = ...``, else None."""
    for node in module.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        else:
            continue
        if isinstance(target, ast.Name) and target.id == name and value is not None:
            try:
                return ast.literal_eval(value)
            except ValueError:
                return None
    return None


def _revision_graph(versions_dir: Path) -> dict[str, _Revision]:
    """``{revision: _Revision}`` for every alembic file in ``versions_dir``."""
    graph: dict[str, _Revision] = {}
    for path in sorted(versions_dir.glob("*.py")):
        module = ast.parse(path.read_text(encoding="utf-8"))
        revision = _assigned_literal(module, "revision")
        if not isinstance(revision, str):
            continue
        down = _assigned_literal(module, "down_revision")
        parents = (down,) if isinstance(down, str) else tuple(down or ())
        upgrade = next(
            (
                node
                for node in module.body
                if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
            ),
            None,
        )
        graph[revision] = _Revision(parents, upgrade, module)
    return graph


def _pending_revisions(
    graph: Mapping[str, _Revision], applied_heads: Sequence[str]
) -> dict[str, _Revision]:
    """Every revision newer than the applied heads; empty when a head is unknown.

    One unknown head makes ``alembic upgrade head`` fail, so nothing is covered then.
    """
    if not applied_heads or any(head not in graph for head in applied_heads):
        return {}
    children: dict[str, list[str]] = {}
    for revision, entry in graph.items():
        for parent in entry.parents:
            children.setdefault(parent, []).append(revision)
    pending: dict[str, _Revision] = {}
    queue = list(applied_heads)
    while queue:
        for child in children.get(queue.pop(), ()):
            if child not in pending:
                pending[child] = graph[child]
                queue.append(child)
    return pending


def _enum_call_name(node: ast.Call) -> str | None:
    """The ``name=`` of a call to ``ENUM(...)`` or ``Enum(...)``, else None."""
    func = node.func
    called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    if called not in ("ENUM", "Enum"):
        return None
    for keyword in node.keywords:
        if (
            keyword.arg == "name"
            and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, str)
        ):
            return keyword.value.value
    return None


def _without_downgrade(module: ast.Module) -> list[ast.AST]:
    nodes: list[ast.AST] = []
    for top in module.body:
        if isinstance(top, ast.FunctionDef) and top.name == "downgrade":
            continue
        nodes.extend(ast.walk(top))
    return nodes


def _enum_evidence(pending: Mapping[str, _Revision], enum_name: str) -> _EnumEvidence:
    """What the pending ``upgrade()`` statements do to one enum type.

    Evidence is code, not text: a comment, ``downgrade()`` and an unrelated string never
    count. A type exists after ``upgrade()`` when an ``ENUM(..., name=<type>)`` call
    creates it (``op.create_table`` and friends) or, with ``create_type=False``, a
    ``.create(...)`` call runs. A label is added by ``ALTER TYPE <type> ADD VALUE '<label>'``.
    """
    name = re.escape(enum_name)
    alter = re.compile(
        rf"""\bALTER\s+TYPE\s+"?{name}"?\s+ADD\s+VALUE\s+(?:IF\s+NOT\s+EXISTS\s+)?'([^']+)'""",
        re.IGNORECASE,
    )
    drop = re.compile(
        rf"""\bDROP\s+TYPE\s+(?:IF\s+EXISTS\s+)?"?{name}"?(?![\w])""", re.IGNORECASE
    )
    evidence = _EnumEvidence()
    for revision_id, revision in pending.items():
        if revision.upgrade is None:
            continue
        upgrade_nodes = list(ast.walk(revision.upgrade))
        explicit_create = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create"
            for node in upgrade_nodes
        )
        for node in upgrade_nodes:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                added = alter.findall(node.value)
                evidence.added.update(added)
                evidence.dropped |= bool(drop.search(node.value))
                if added:
                    evidence.revisions.add(revision_id)
        for node in _without_downgrade(revision.module):
            if not isinstance(node, ast.Call) or _enum_call_name(node) != enum_name:
                continue
            no_auto_create = any(
                keyword.arg == "create_type"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is False
                for keyword in node.keywords
            )
            if no_auto_create and not explicit_create:
                continue
            evidence.creates = True
            evidence.revisions.add(revision_id)
            evidence.created.update(
                arg.value
                for arg in node.args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            )
    return evidence


def cover_with_pending_migrations(
    discrepancies: Sequence[EnumDiscrepancy],
    *,
    applied_heads: Sequence[str],
    versions_dir: Path,
) -> tuple[list[EnumDiscrepancy], list[tuple[str, str]]]:
    """Split off the additive discrepancies that a pending migration will fix (#1658).

    The image runs ``alembic upgrade head`` after this gate. A type that the database
    lacks is covered when the pending ``upgrade()`` statements create it with every
    missing label. A label that an existing type lacks is covered when they add it with
    ``ALTER TYPE ... ADD VALUE``. A type that a pending revision drops is never covered.
    Returns ``(blocking, covered)`` with ``covered`` as ``(enum name, revisions)``.
    Only ``missing_in_db`` can be covered; a DB-only label and a casing drift always
    block. The caller treats any error here (an unreadable or invalid file) as "nothing
    covered".
    """
    pending = _pending_revisions(_revision_graph(versions_dir), applied_heads)
    blocking: list[EnumDiscrepancy] = []
    covered: list[tuple[str, str]] = []
    for discrepancy in discrepancies:
        evidence = _enum_evidence(pending, discrepancy.enum_name)
        missing = set(discrepancy.missing_in_db)
        if not discrepancy.db_values:
            fixed = evidence.creates and missing <= evidence.created | evidence.added
        else:
            fixed = missing <= evidence.added
        if (
            missing
            and fixed
            and not evidence.dropped
            and not discrepancy.missing_in_code
            and not discrepancy.casing_mismatches
        ):
            covered.append(
                (discrepancy.enum_name, ",".join(sorted(evidence.revisions)))
            )
        else:
            blocking.append(discrepancy)
    return blocking, covered


def _resolve(reference: str) -> Any:
    module_name, _, attribute_path = reference.partition(":")
    target: Any = importlib.import_module(module_name)
    for part in filter(None, attribute_path.split(".")):
        target = getattr(target, part)
    return target


def _enum_type(column_type: Any) -> Any | None:
    """The native enum type behind a column type, or None.

    Duck-typed on what SQLAlchemy's ``Enum`` exposes (``name``, ``enums``,
    ``native_enum``) so infra2 does not need SQLAlchemy installed; an ``ARRAY`` of an
    enum and a ``TypeDecorator`` over one are unwrapped.
    """
    for candidate in (
        column_type,
        getattr(column_type, "item_type", None),
        getattr(column_type, "impl_instance", None),
    ):
        if candidate is None:
            continue
        labels = getattr(candidate, "enums", None)
        if (
            isinstance(labels, (list, tuple))
            and getattr(candidate, "name", None)
            and getattr(candidate, "native_enum", True)
        ):
            return candidate
    return None


def enums_from_metadata(metadata: Any) -> dict[str, tuple[str, ...]]:
    """``{postgres enum type name: labels}`` for every native enum column on ``metadata``."""
    tables = getattr(metadata, "tables", None)
    if not isinstance(tables, Mapping):
        raise GateNotEvaluated(
            f"{type(metadata).__name__} is not a SQLAlchemy MetaData (no .tables)"
        )
    enums: dict[str, tuple[str, ...]] = {}
    for table in tables.values():
        for column in table.columns:
            enum_type = _enum_type(column.type)
            if enum_type is None:
                continue
            labels = tuple(str(label) for label in enum_type.enums)
            known = enums.setdefault(enum_type.name, labels)
            if set(known) != set(labels):
                raise GateNotEvaluated(
                    f"enum type {enum_type.name!r} is declared with different labels "
                    f"on two columns: {sorted(known)} vs {sorted(labels)}"
                )
    return enums


def load_code_enums_for_service(
    service: str,
    *,
    app_path: str | None = None,
    sources: Mapping[str, EnumSource] | None = None,
) -> dict[str, tuple[str, ...]]:
    """The service's code-side enums; raises ``GateNotEvaluated`` when they can't be read.

    An unregistered service, a module that does not import, or a metadata object with
    no native enum types all mean nothing can be compared — never "0 discrepancies".
    """
    sources = ENUM_SOURCES if sources is None else sources
    source = sources.get(service)
    if source is None:
        raise GateNotEvaluated(
            f"no enum source is registered for {service!r} "
            f"(registered: {', '.join(sorted(sources)) or 'none'})"
        )
    if app_path:
        directory = Path(app_path).expanduser().resolve()
        if not directory.is_dir():
            raise GateNotEvaluated(f"--app-path {app_path} is not a directory")
        if str(directory) not in sys.path:
            sys.path.insert(0, str(directory))
    try:
        for module_name in source.imports:
            importlib.import_module(module_name)
        metadata = _resolve(source.metadata)
    except Exception as exc:  # noqa: BLE001 - any import-time failure means "not loaded"
        hint = f" --app-path {source.app_path}" if source.app_path else ""
        raise GateNotEvaluated(
            f"cannot load {service} enums from {source.metadata}: "
            f"{type(exc).__name__}: {exc} (run it in the service's own Python "
            f"environment{hint})"
        ) from exc
    enums = enums_from_metadata(metadata)
    if not enums:
        raise GateNotEvaluated(
            f"{source.metadata} declares no native enum types for {service}; "
            "an empty code side cannot be compared"
        )
    return enums


def libpq_url(db_url: str) -> str:
    """Drop a SQLAlchemy driver suffix (``postgresql+asyncpg://``) libpq rejects."""
    return re.sub(r"^(postgres(?:ql)?)\+[A-Za-z0-9_]+://", r"\1://", db_url)


_SAFE_SQL_IDENTIFIER_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_.]*\Z")
_READ_ONLY_SQL_RE = re.compile(r"\A\s*SELECT\b", re.IGNORECASE)
_FORBIDDEN_SQL_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)


def query_applied_migrations(
    connection_or_cursor: Any,
    *,
    migration_table: str = "alembic_version",
    migration_column: str = "version_num",
) -> tuple[str, ...]:
    """Query the applied migration versions recorded in the target database (#609)."""
    if not _SAFE_SQL_IDENTIFIER_RE.match(
        migration_table
    ) or not _SAFE_SQL_IDENTIFIER_RE.match(migration_column):
        raise ValueError(
            f"invalid migration table/column identifier: {migration_table}.{migration_column}"
        )
    cursor = (
        connection_or_cursor.cursor()
        if hasattr(connection_or_cursor, "cursor")
        else connection_or_cursor
    )
    cursor.execute(
        f"SELECT {migration_column} FROM {migration_table} ORDER BY {migration_column};"
    )
    rows = cursor.fetchall()
    versions: list[str] = []
    for row in rows or ():
        val = row[0] if isinstance(row, (tuple, list)) else row.get(migration_column)
        if val is not None and str(val).strip():
            versions.append(str(val).strip())
    return tuple(versions)


def check_data_preconditions(
    connection_or_cursor: Any,
    *,
    requires_migration: str | None = None,
    migration_table: str = "alembic_version",
    migration_column: str = "version_num",
    preconditions: Sequence[tuple[str, str, str]] = (),
) -> tuple[tuple[str, ...], list[str]]:
    """Verify migration floor and named read-only SQL data preconditions (#609).

    Returns ``(applied_migrations, failures)``. Fails closed when ``requires_migration``
    is unmet or any read-only SQL predicate returns false/0/None or errors.
    """
    applied: tuple[str, ...] = ()
    failures: list[str] = []
    cursor = (
        connection_or_cursor.cursor()
        if hasattr(connection_or_cursor, "cursor")
        else connection_or_cursor
    )
    if requires_migration is not None:
        try:
            applied = query_applied_migrations(
                cursor,
                migration_table=migration_table,
                migration_column=migration_column,
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(
                f"migration_floor ({requires_migration}): could not query "
                f"{migration_table}.{migration_column}: {exc}"
            )
        else:
            if not applied:
                failures.append(
                    f"migration_floor ({requires_migration}): {migration_table} is empty "
                    f"(required >= {requires_migration})"
                )
            elif max(applied) < str(requires_migration).strip():
                failures.append(
                    f"migration_floor ({requires_migration}): applied migration "
                    f"{max(applied)!r} is older than required floor {requires_migration!r}"
                )

    for name, sql_predicate, remedy in preconditions:
        if not _READ_ONLY_SQL_RE.match(sql_predicate or "") or _FORBIDDEN_SQL_RE.search(
            sql_predicate or ""
        ):
            failures.append(
                f"{name}: precondition query must be a read-only SELECT statement"
            )
            continue
        try:
            cursor.execute(sql_predicate)
            rows = cursor.fetchall()
            scalar = (
                rows[0][0]
                if rows and isinstance(rows[0], (tuple, list)) and rows[0]
                else (
                    next(iter(rows[0].values()))
                    if rows and isinstance(rows[0], Mapping) and rows[0]
                    else None
                )
            )
            satisfied = bool(scalar) and str(scalar).strip().lower() not in (
                "false",
                "f",
                "0",
                "no",
                "none",
                "",
            )
            if not satisfied:
                remedy_suffix = f" — remediation: {remedy}" if remedy else ""
                failures.append(
                    f"{name}: predicate ({sql_predicate}) returned {scalar!r}{remedy_suffix}"
                )
        except Exception as exc:  # noqa: BLE001
            remedy_suffix = f" — remediation: {remedy}" if remedy else ""
            failures.append(f"{name}: query failed ({exc}){remedy_suffix}")

    return applied, failures


def _with_libpq_cursor(db_url: str, fn: Any) -> Any:
    """Open one libpq (psycopg/psycopg2) connection and pass a cursor to ``fn``."""
    url = libpq_url(db_url)
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError:
        import psycopg2  # type: ignore[import-not-found]

        with psycopg2.connect(url) as conn:
            with conn.cursor() as cur:
                return fn(cur)
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            return fn(cur)


def fetch_db_enums(db_url: str) -> dict[str, list[str]]:
    return _with_libpq_cursor(db_url, query_db_enums)


def evaluate_db_preconditions(
    db_url: str, source: EnumSource
) -> tuple[tuple[str, ...], list[str]]:
    """Run declarative migration-floor and SQL data preconditions when configured (#609)."""
    if source.requires_migration is None and not source.preconditions:
        return (), []
    return _with_libpq_cursor(
        db_url,
        lambda cur: check_data_preconditions(
            cur,
            requires_migration=source.requires_migration,
            migration_table=source.migration_table,
            migration_column=source.migration_column,
            preconditions=source.preconditions,
        ),
    )


def fetch_applied_heads(db_url: str, source: EnumSource) -> tuple[str, ...]:
    """The alembic heads recorded in the target database (#1658)."""
    return _with_libpq_cursor(
        db_url,
        lambda cur: query_applied_migrations(
            cur,
            migration_table=source.migration_table,
            migration_column=source.migration_column,
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pre-deploy schema & enum check gate (#698)"
    )
    parser.add_argument(
        "--service",
        required=True,
        help=f"Target service, one of: {', '.join(sorted(ENUM_SOURCES))}",
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help="Postgres connection URL (default: $DATABASE_URL); required",
    )
    parser.add_argument(
        "--app-path",
        default=None,
        help="Directory put first on sys.path so the service's own package imports",
    )
    args = parser.parse_args(argv)

    db_url = args.db_url or os.environ.get("DATABASE_URL")
    reasons: list[str] = []
    if not db_url:
        reasons.append("no database URL (--db-url or DATABASE_URL)")
    code_enums: dict[str, tuple[str, ...]] = {}
    try:
        code_enums = load_code_enums_for_service(args.service, app_path=args.app_path)
    except GateNotEvaluated as exc:
        reasons.append(str(exc))
    if reasons:
        print(
            f"NOT EVALUATED — deploy blocked: pre-deploy schema check for {args.service}:",
            file=sys.stderr,
        )
        for reason in reasons:
            print(f"  - {reason}", file=sys.stderr)
        return EXIT_NOT_EVALUATED

    try:
        db_enums = fetch_db_enums(db_url)
        source = ENUM_SOURCES.get(args.service)
        applied_migrations: tuple[str, ...] = ()
        precondition_failures: list[str] = []
        if source is not None:
            applied_migrations, precondition_failures = evaluate_db_preconditions(
                db_url, source
            )
    except Exception as exc:
        print(
            f"ERROR: Failed to connect to DB for pre-deploy schema check: {exc}",
            file=sys.stderr,
        )
        return EXIT_BLOCKED

    if applied_migrations:
        print(f"APPLIED_MIGRATION: {','.join(applied_migrations)}")

    discrepancies = check_enums(code_enums, db_enums)
    covered: list[tuple[str, str]] = []
    if discrepancies and source is not None and source.migrations_path:
        base = Path(args.app_path).expanduser() if args.app_path else Path.cwd()
        try:
            discrepancies, covered = cover_with_pending_migrations(
                discrepancies,
                applied_heads=fetch_applied_heads(db_url, source),
                versions_dir=base / source.migrations_path,
            )
        except Exception:  # noqa: BLE001 - fail closed: an unreadable head or file covers nothing
            covered = []
    for enum_name, revision in covered:
        print(f"PENDING_MIGRATION_COVERS: {enum_name} ({revision})")
    # Printed on stdout (never stderr) in both branches below, as a stable-format line a
    # caller can grep for, regardless of whether the gate itself passed or blocked. This
    # process normally runs remotely, over SSH inside the app's own container
    # (common/deploy/schema_gate.py's module docstring) -- it is THAT module's
    # run_schema_gate() that parses this exact line back out of the captured stdout and
    # returns/reports it; common/deploy/promote.py's deploy() then carries the value into
    # DeployPlan.rollback_class, and tools/deploy_v2.py exposes it in its own JSON
    # result as detail["rollback_class"]. This script itself has no caller here — it
    # only has to keep emitting a predictable line for schema_gate.py to find.
    rollback_class = classify_rollback(discrepancies)
    if discrepancies or precondition_failures:
        if discrepancies:
            print(
                f"ERROR: Schema discrepancies found for {args.service}:",
                file=sys.stderr,
            )
            for disc in discrepancies:
                print(
                    f"  - {disc.enum_name}: missing_in_db={disc.missing_in_db}, "
                    f"missing_in_code={disc.missing_in_code}, casing={disc.casing_mismatches}",
                    file=sys.stderr,
                )
        if precondition_failures:
            print(
                f"ERROR: Data preconditions failed for {args.service}:",
                file=sys.stderr,
            )
            for failure in precondition_failures:
                print(f"  - {failure}", file=sys.stderr)
        print(f"ROLLBACK_CLASS: {rollback_class}")
        return EXIT_BLOCKED

    print(
        f"Pre-deploy schema check verified for service: {args.service} "
        f"({len(code_enums)} enum types, 0 discrepancies"
        f"{f', {len(covered)} covered by pending migrations' if covered else ''})"
    )
    print(f"ROLLBACK_CLASS: {rollback_class}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
