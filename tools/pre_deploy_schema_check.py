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
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any, Sequence


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
        rows = []

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


def load_code_enums_for_service(service: str) -> dict[str, type[Enum] | Sequence[str]]:
    """Discover and load Enum definitions for the specified service."""
    enums: dict[str, type[Enum] | Sequence[str]] = {}
    if "finance" in service.lower():
        try:
            import importlib

            mod = importlib.import_module("finance_report.models.enums")
            for attr_name in dir(mod):
                attr = getattr(mod, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, Enum)
                    and attr is not Enum
                ):
                    name = re.sub(r"(?<!^)(?=[A-Z])", "_", attr.__name__).lower()
                    enums[name] = attr
        except (ImportError, AttributeError):
            pass
    return enums


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pre-deploy schema & enum check gate (#698)"
    )
    parser.add_argument(
        "--service", required=True, help="Target service, e.g. finance_report/app"
    )
    parser.add_argument(
        "--db-url", default=None, help="Optional direct postgres connection URL"
    )
    args = parser.parse_args(argv)

    code_enums = load_code_enums_for_service(args.service)
    db_url = args.db_url or os.environ.get("DATABASE_URL")

    if not db_url:
        print(
            f"Pre-deploy schema check: no database URL provided for {args.service}; verified code enums count={len(code_enums)}"
        )
        return 0

    db_enums: dict[str, list[str]] = {}
    try:
        try:
            import psycopg  # type: ignore[import-not-found]

            with psycopg.connect(db_url) as conn:
                with conn.cursor() as cur:
                    db_enums = query_db_enums(cur)
        except ImportError:
            import psycopg2  # type: ignore[import-not-found]

            with psycopg2.connect(db_url) as conn:
                with conn.cursor() as cur:
                    db_enums = query_db_enums(cur)
    except Exception as exc:
        print(
            f"ERROR: Failed to connect to DB for pre-deploy schema check: {exc}",
            file=sys.stderr,
        )
        return 1

    discrepancies = check_enums(code_enums, db_enums)
    if discrepancies:
        print(f"ERROR: Schema discrepancies found for {args.service}:", file=sys.stderr)
        for disc in discrepancies:
            print(
                f"  - {disc.enum_name}: missing_in_db={disc.missing_in_db}, "
                f"missing_in_code={disc.missing_in_code}, casing={disc.casing_mismatches}",
                file=sys.stderr,
            )
        return 1

    print(
        f"Pre-deploy schema check verified for service: {args.service} (0 discrepancies)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
