#!/usr/bin/env python3
"""Infrastructure asset reachability audit tool (SSOT Mechanism 2 / #822).

Enforces that any persistent infrastructure asset (database, bucket, host data path)
must be statically reachable from service registry declarations (Deployers, Facets, Composes).
Assets outside the authorized sets are flagged as orphans for decommissioning.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from libs.service_registry import (  # noqa: E402
    bootstrap_facet_attrs,
    service_attrs,
)


def get_authorized_data_paths() -> frozenset[str]:
    """Extract authorized data paths from service_attrs() and bootstrap_facet_attrs()."""
    attrs = {**service_attrs(), **bootstrap_facet_attrs()}
    paths: set[str] = set()
    for meta in attrs.values():
        if meta.data_path:
            paths.add(meta.data_path.rstrip("/"))
        for facet in meta.backups:
            if facet.data_path:
                paths.add(facet.data_path.rstrip("/"))
    return frozenset(paths)


# Static / Registry-derived canonical authorized sets (SSOT #822)
AUTHORIZED_DATABASES: frozenset[str] = frozenset(
    {
        "postgres",
        "authentik",
        "openpanel",
        "signoz",
        "finance_report",
        "truealpha",
        "prefect",
        "template0",
        "template1",
    }
)

AUTHORIZED_BUCKETS: frozenset[str] = frozenset(
    {
        "finance-report",
        "finance-report-staging",
        "finance-report-backup",
        "openpanel",
        "signoz",
        "authentik-media",
        "statements",
        "archive",
    }
)

AUTHORIZED_DATA_PATHS: frozenset[str] = get_authorized_data_paths()


def audit_databases(
    databases: list[str],
    *,
    authorized: frozenset[str] | set[str] | None = None,
) -> list[str]:
    """Audit database names against the authorized whitelist and return orphans."""
    auth = authorized if authorized is not None else AUTHORIZED_DATABASES
    return sorted(db for db in databases if db not in auth)


def audit_buckets(
    buckets: list[str],
    *,
    authorized: frozenset[str] | set[str] | None = None,
) -> list[str]:
    """Audit object storage buckets against the authorized whitelist and return orphans."""
    auth = authorized if authorized is not None else AUTHORIZED_BUCKETS
    return sorted(b for b in buckets if b not in auth)


def audit_paths(
    paths: list[str],
    *,
    authorized: frozenset[str] | set[str] | None = None,
) -> list[str]:
    """Audit persistent data paths against the authorized whitelist and return orphans."""
    auth = authorized if authorized is not None else AUTHORIZED_DATA_PATHS
    normalized_auth = {p.rstrip("/") for p in auth}
    return sorted(p for p in paths if p.rstrip("/") not in normalized_auth)


@dataclass(frozen=True)
class AuditReport:
    orphan_databases: list[str]
    orphan_buckets: list[str]
    orphan_paths: list[str]
    dry_run: bool = False

    @property
    def has_orphans(self) -> bool:
        return bool(self.orphan_databases or self.orphan_buckets or self.orphan_paths)

    def to_dict(self) -> dict[str, Any]:
        return {
            "orphan_databases": self.orphan_databases,
            "orphan_buckets": self.orphan_buckets,
            "orphan_paths": self.orphan_paths,
            "has_orphans": self.has_orphans,
            "dry_run": self.dry_run,
        }


def run_audit(
    *,
    databases: list[str] | None = None,
    buckets: list[str] | None = None,
    paths: list[str] | None = None,
    dry_run: bool = False,
) -> AuditReport:
    orphan_dbs = audit_databases(databases or [])
    orphan_bks = audit_buckets(buckets or [])
    orphan_pts = audit_paths(paths or [])
    return AuditReport(
        orphan_databases=orphan_dbs,
        orphan_buckets=orphan_bks,
        orphan_paths=orphan_pts,
        dry_run=dry_run,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit infrastructure assets for SSOT reachability against service registry declarations."
    )
    parser.add_argument(
        "--databases",
        nargs="*",
        default=None,
        help="List of database names to audit",
    )
    parser.add_argument(
        "--buckets",
        nargs="*",
        default=None,
        help="List of bucket names to audit",
    )
    parser.add_argument(
        "--paths",
        nargs="*",
        default=None,
        help="List of filesystem paths to audit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform audit without operational side effects",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Output report as JSON",
    )
    parser.add_argument(
        "--fail-on-orphans",
        action="store_true",
        help="Exit with non-zero code if any orphan asset is found",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    report = run_audit(
        databases=args.databases,
        buckets=args.buckets,
        paths=args.paths,
        dry_run=args.dry_run,
    )

    if args.json_output:
        data = report.to_dict()
        data["authorized_databases"] = sorted(AUTHORIZED_DATABASES)
        data["authorized_buckets"] = sorted(AUTHORIZED_BUCKETS)
        data["authorized_data_paths"] = sorted(AUTHORIZED_DATA_PATHS)
        print(json.dumps(data, indent=2))
    else:
        prefix = "[DRY-RUN] " if args.dry_run else ""
        print(f"{prefix}=== Asset Reachability Audit Report ===")
        print(
            f"Orphan databases ({len(report.orphan_databases)}): {', '.join(report.orphan_databases) or 'none'}"
        )
        print(
            f"Orphan buckets ({len(report.orphan_buckets)}): {', '.join(report.orphan_buckets) or 'none'}"
        )
        print(
            f"Orphan data paths ({len(report.orphan_paths)}): {', '.join(report.orphan_paths) or 'none'}"
        )

    if args.fail_on_orphans and report.has_orphans:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
