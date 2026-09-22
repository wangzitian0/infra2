"""Unit tests for tools/asset_reachability_audit.py (SSOT #822).

Validates:
- Extraction of authorized databases, buckets, and paths from service registry.
- Precision of orphan detection (e.g. activepieces, appwrite-0).
- CLI argument parsing, JSON output, and exit codes.
"""

from __future__ import annotations

import json
from io import StringIO
from unittest.mock import patch

from tools.asset_reachability_audit import (
    AUTHORIZED_BUCKETS,
    AUTHORIZED_DATA_PATHS,
    AUTHORIZED_DATABASES,
    audit_buckets,
    audit_databases,
    audit_paths,
    main,
)


def test_authorized_whitelists_not_empty() -> None:
    """Verify that authorized asset sets are populated from declarations."""
    assert len(AUTHORIZED_DATABASES) > 0
    assert len(AUTHORIZED_BUCKETS) > 0
    assert len(AUTHORIZED_DATA_PATHS) > 0

    # Ensure known services are present
    assert "postgres" in AUTHORIZED_DATABASES
    assert "finance_report" in AUTHORIZED_DATABASES
    assert "truealpha" in AUTHORIZED_DATABASES
    assert "authentik" in AUTHORIZED_DATABASES
    assert "signoz" in AUTHORIZED_DATABASES
    assert "openpanel" in AUTHORIZED_DATABASES

    # Ensure deleted/orphan services are NOT in the whitelist
    assert "activepieces" not in AUTHORIZED_DATABASES

    # Buckets
    assert "finance-report" in AUTHORIZED_BUCKETS
    assert "openpanel" in AUTHORIZED_BUCKETS
    assert "signoz" in AUTHORIZED_BUCKETS
    assert "authentik-media" in AUTHORIZED_BUCKETS
    assert "truealpha-raw" in AUTHORIZED_BUCKETS
    assert "truealpha-staging-raw" in AUTHORIZED_BUCKETS
    assert "appwrite-0" not in AUTHORIZED_BUCKETS
    assert "appwrite-staging-0" not in AUTHORIZED_BUCKETS

    # Paths
    assert "/data/platform/postgres" in AUTHORIZED_DATA_PATHS
    assert "/data/platform/activepieces" not in AUTHORIZED_DATA_PATHS


def test_audit_databases_identifies_activepieces_as_orphan() -> None:
    """Verifies that audit_databases precision flags activepieces."""
    candidates = ["postgres", "activepieces", "finance_report"]
    orphans = audit_databases(candidates)
    assert orphans == ["activepieces"]

    # When all are authorized, returns empty list
    clean = ["postgres", "finance_report", "truealpha"]
    assert audit_databases(clean) == []


def test_audit_buckets_identifies_appwrite_as_orphan() -> None:
    """Verifies that audit_buckets precision flags appwrite-0."""
    candidates = ["signoz", "appwrite-0"]
    orphans = audit_buckets(candidates)
    assert orphans == ["appwrite-0"]

    # Clean buckets
    clean = ["signoz", "openpanel", "finance-report"]
    assert audit_buckets(clean) == []


def test_audit_paths_identifies_orphan_paths() -> None:
    """Verifies that audit_paths flags unmanaged host directories."""
    candidates = [
        "/data/platform/postgres",
        "/data/platform/activepieces",
        "/data/platform/redis/",
    ]
    orphans = audit_paths(candidates)
    assert orphans == ["/data/platform/activepieces"]


def test_cli_main_clean_exit_code() -> None:
    """Verifies CLI exit code 0 when all passed assets are authorized."""
    exit_code = main(["--databases", "postgres", "truealpha", "--fail-on-orphans"])
    assert exit_code == 0


def test_cli_main_fail_on_orphans_exit_code() -> None:
    """Verifies CLI exits with code 1 when orphans are found with --fail-on-orphans."""
    exit_code = main(["--databases", "postgres", "activepieces", "--fail-on-orphans"])
    assert exit_code == 1

    # Without --fail-on-orphans, it reports orphans but exits with code 0
    exit_code_lenient = main(["--databases", "postgres", "activepieces"])
    assert exit_code_lenient == 0


def test_cli_main_zero_inputs_exits_2() -> None:
    """Verifies CLI exits with code 2 when no candidate assets are specified (anti-green-while-empty)."""
    assert main([]) == 2


def test_cli_main_json_and_dry_run_output() -> None:
    """Verifies CLI JSON output format and dry-run flag."""
    captured = StringIO()
    with patch("sys.stdout", captured):
        exit_code = main(["--databases", "activepieces", "--dry-run", "--json"])
    assert exit_code == 0

    data = json.loads(captured.getvalue())
    assert data["dry_run"] is True
    assert data["has_orphans"] is True
    assert data["orphan_databases"] == ["activepieces"]
    assert "authorized_databases" in data
    assert "authorized_buckets" in data
    assert "authorized_data_paths" in data
