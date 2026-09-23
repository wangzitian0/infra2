"""Pre-deploy schema & enum consistency check gate (#698).

Validates that application Python ORM / data model Enum definitions **strictly match**
the database enum types before a deploy destroys or restarts running containers.
During the 2026-09-10 incident (#698), an enum case mismatch ('PENDING' vs 'pending')
caused finance_report backend to fail during startup, bringing the service down for
13 minutes because containers were removed before verifying schema compatibility.

Audit-hardened (反事实审计 2026-09-16):
- Bidirectional comparison: missing_in_db AND missing_in_code both block deploy.
  DB superset is the most dangerous runtime divergence.
- Fail-closed: any exception (query failure, parse error) blocks deploy.
  A check that silently fails is worse than no check.
- Never a silent pass (#718 review): a gate that cannot load its code-side enums or
  has no database to compare against has not evaluated anything, so it exits
  ``EXIT_NOT_EVALUATED`` — non-zero, like a finding.

Exit codes: 0 verified (0 discrepancies) · 1 blocked (discrepancy or DB failure) ·
3 not evaluated (an input is unavailable) — also blocking.

The code side is the service's own SQLAlchemy metadata, so the gate runs where the
application imports (its image or virtualenv), not from infra2's environment::

    python tools/pre_deploy_schema_check.py --service finance_report/app
        --app-path <finance_report checkout>/apps/backend --db-url postgresql://...
"""

from __future__ import annotations

import argparse
import importlib
import os
import re
import sys
from dataclasses import dataclass
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
    """Where a service's code-side enum truth lives.

    ``metadata`` names a SQLAlchemy ``MetaData`` as ``module:attribute.path``; every
    native enum column type registered on it is one Postgres enum type (its ``name``)
    with its labels (``enums``). ``imports`` are imported first, for their side effect
    of registering every mapped class on that metadata. ``app_path`` says which
    directory of the service's checkout has to be on ``sys.path``.
    """

    metadata: str
    imports: tuple[str, ...] = ()
    app_path: str = ""


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


def fetch_db_enums(db_url: str) -> dict[str, list[str]]:
    url = libpq_url(db_url)
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError:
        import psycopg2  # type: ignore[import-not-found]

        with psycopg2.connect(url) as conn:
            with conn.cursor() as cur:
                return query_db_enums(cur)
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            return query_db_enums(cur)


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
    except Exception as exc:
        print(
            f"ERROR: Failed to connect to DB for pre-deploy schema check: {exc}",
            file=sys.stderr,
        )
        return EXIT_BLOCKED

    discrepancies = check_enums(code_enums, db_enums)
    # Printed on stdout (never stderr) in both branches below, as a stable-format line a
    # caller can grep for, regardless of whether the gate itself passed or blocked. This
    # process normally runs remotely, over SSH inside the app's own container
    # (libs/deploy/schema_gate.py's module docstring) -- it is THAT module's
    # run_schema_gate() that parses this exact line back out of the captured stdout and
    # returns/reports it; libs/deploy/promote.py's deploy() then carries the value into
    # DeployPlan.rollback_class, and tools/deploy_v2.py exposes it in its own JSON
    # result as detail["rollback_class"]. This script itself has no caller here — it
    # only has to keep emitting a predictable line for schema_gate.py to find.
    rollback_class = classify_rollback(discrepancies)
    if discrepancies:
        print(f"ERROR: Schema discrepancies found for {args.service}:", file=sys.stderr)
        for disc in discrepancies:
            print(
                f"  - {disc.enum_name}: missing_in_db={disc.missing_in_db}, "
                f"missing_in_code={disc.missing_in_code}, casing={disc.casing_mismatches}",
                file=sys.stderr,
            )
        print(f"ROLLBACK_CLASS: {rollback_class}")
        return EXIT_BLOCKED

    print(
        f"Pre-deploy schema check verified for service: {args.service} "
        f"({len(code_enums)} enum types, 0 discrepancies)"
    )
    print(f"ROLLBACK_CLASS: {rollback_class}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
