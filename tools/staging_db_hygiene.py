#!/usr/bin/env python3
"""Staging database retention runner for finance_report (#1624 AC2).

Invokes finance_report's own `scripts/purge_test_accounts.py --rotate-append-only`
inside the staging backend container and prints before/after PostgreSQL database sizes.
"""

from __future__ import annotations

import argparse
import shlex
import sys

from common.deploy.vps_ssh import resolve_ssh_credentials, ssh_cmd

STAGING_BACKEND_CONTAINER = "finance-report-backend-staging"
STAGING_POSTGRES_CONTAINER = "finance_report-postgres-staging"
STAGING_DATABASE = "finance_report"
DEFAULT_RETENTION_DAYS = 14


def build_remote_command(*, older_than_days: int, apply: bool) -> str:
    """Build the VPS SSH command for staging test-account and trace_records retention."""
    if older_than_days < 1:
        raise ValueError(f"older_than_days must be >= 1, got {older_than_days}")
    size_sql = f"SELECT pg_size_pretty(pg_database_size('{STAGING_DATABASE}')), pg_database_size('{STAGING_DATABASE}');"
    size_cmd = (
        f"docker exec {STAGING_POSTGRES_CONTAINER} "
        f"psql -U postgres -d {STAGING_DATABASE} -tAc {shlex.quote(size_sql)}"
    )
    purge_parts = [
        "docker",
        "exec",
        STAGING_BACKEND_CONTAINER,
        "python",
        "scripts/purge_test_accounts.py",
        "--rotate-append-only",
        "--older-than-days",
        str(older_than_days),
    ]
    if apply:
        purge_parts.append("--apply")
    purge_cmd = shlex.join(purge_parts)
    return (
        f'echo "=== SIZE_BEFORE ===" && {size_cmd} && '
        f'echo "=== PURGE ===" && {purge_cmd} && '
        f'echo "=== SIZE_AFTER ===" && {size_cmd}'
    )


def run_staging_db_hygiene(
    *,
    older_than_days: int = DEFAULT_RETENTION_DAYS,
    apply: bool = False,
    runner=ssh_cmd,
) -> int:
    """Run staging DB retention over SSH and print output."""
    host, user, key_path, port = resolve_ssh_credentials()
    command = build_remote_command(older_than_days=older_than_days, apply=apply)
    code, stdout, stderr = runner(command, host, user, key_path, port)
    if stdout:
        print(stdout, end="" if stdout.endswith("\n") else "\n")
    if stderr:
        print(stderr, file=sys.stderr, end="" if stderr.endswith("\n") else "\n")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run finance_report staging test-account and trace_records cleanup."
    )
    parser.add_argument(
        "--older-than-days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help="Retention threshold in days (default: 14).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply deletions (default is dry-run).",
    )
    args = parser.parse_args(argv)
    return run_staging_db_hygiene(
        older_than_days=args.older_than_days, apply=args.apply
    )


if __name__ == "__main__":
    raise SystemExit(main())
