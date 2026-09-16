"""Tests for pre-deploy schema & enum check gate (#698)."""

from enum import Enum

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
