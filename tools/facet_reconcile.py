#!/usr/bin/env python3
"""One daily reality-vs-declaration reconcile run (#542 task 4).

Absorbs three formerly separate drift checkers — each was its own scheduled
job with its own hand-copied alert step:

- **compose_id drift** (`tools/app_compose_id_drift.py`, was ops-checks cron
  ``17 7 * * *``): hardcoded bespoke-app Dokploy compose_ids vs live Dokploy.
- **config drift** (`tools/dokploy_config_drift.py`, was
  ``config-drift-report.yml``): declared IaC source-config hash at the latest
  release tag vs the deployed hash.
- **dns drift** (`tools/dns_drift_report.py`, was ``dns-drift-report.yml``):
  declared DNS records vs live Cloudflare.

Each absorbed tool stays a library (its own tests keep passing; operators can
still run it standalone) — what died is the three separate workflow
jobs/crons/alert-step copies.

Deliberately NOT absorbed: `tools/preview_leak_check.py`. It is an HOURLY
lifecycle-contract check (live previews vs live PR state — reality vs reality,
no declaration involved), not facet reconciliation; folding it in here would
silently stretch leak-detection latency 24x.

Report discipline (#425): the combined report is delivered EVERY run, drift or
not — its own arrival self-proves the reconcile path. Paging is separate and
confirmed-only: a transient lookup error fails this job (visible in CI, retried
next schedule) but never pages; only confirmed findings page (#524's
confirmed_drift discipline, preserved per section).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass
class Section:
    name: str
    report: str = ""
    blockers: list[str] = field(default_factory=list)  # fail the job (incl. transient)
    confirmed: list[str] = field(default_factory=list)  # page-worthy findings only
    skipped: str = ""  # non-empty = section not runnable here (with reason)


def run_compose_id_section() -> Section:
    from libs.dokploy import get_dokploy
    from tools.app_compose_id_drift import confirmed_drift, format_report, scan

    section = Section("compose-id")
    try:
        rows = scan(get_dokploy())
    except Exception as exc:  # noqa: BLE001 — a dead Dokploy is a blocker, not a crash
        section.blockers.append(f"compose_id scan failed: {exc}")
        section.report = f"compose-id scan failed: {exc}"
        return section
    section.report = format_report(rows)
    section.blockers = [r.verdict for r in rows if r.verdict != "ok"]
    section.confirmed = [
        f"{r.target.service} ({r.target.env}): {r.note}" for r in confirmed_drift(rows)
    ]
    return section


def run_config_drift_section() -> Section:
    from tools.dokploy_config_drift import (
        _latest_release_tag,
        format_report,
        scan,
        strict_blockers,
    )

    section = Section("config-hash")
    try:
        tag = _latest_release_tag()
        rows = scan(tag)
    except Exception as exc:  # noqa: BLE001
        section.blockers.append(f"config drift scan failed: {exc}")
        section.report = f"config-hash scan failed: {exc}"
        return section
    section.report = format_report(tag, rows)
    confirmed = strict_blockers(rows)
    section.blockers = [str(r) for r in confirmed]
    section.confirmed = [str(r) for r in confirmed]  # hash drift is never transient
    return section


def run_dns_section() -> Section:
    from tools.dns_drift_report import (
        _actual_records,
        _dns_tasks,
        _expected_records,
        compute_drift,
        format_report,
    )

    section = Section("dns")
    have_zone = bool(os.environ.get("CF_ZONE_ID") or os.environ.get("CF_ZONE_NAME"))
    if not (os.environ.get("CF_API_TOKEN") and os.environ.get("INTERNAL_DOMAIN") and have_zone):
        # preserved semantics: not-yet-configured is a visible skip, never a red
        section.skipped = "Cloudflare credentials not configured"
        section.report = "dns: skipped (Cloudflare credentials not configured)"
        return section
    try:
        dns = _dns_tasks()
        expected = set(_expected_records(dns))
        actual = set(_actual_records(dns))
        drift = compute_drift(expected, actual)
    except Exception as exc:  # noqa: BLE001
        section.blockers.append(f"dns scan failed: {exc}")
        section.report = f"dns scan failed: {exc}"
        return section
    section.report = format_report(
        drift, os.environ.get("INTERNAL_DOMAIN", "?"), len(expected), len(actual)
    )
    # `missing` (declared intent not realized in Cloudflare) is the real signal;
    # `unmanaged` is informational by the tool's own contract — report-only.
    section.blockers = [str(f) for f in drift.missing]
    section.confirmed = [str(f) for f in drift.missing]
    return section


def run_all() -> list[Section]:
    return [run_compose_id_section(), run_config_drift_section(), run_dns_section()]


def combined_report(sections: list[Section]) -> str:
    n_blockers = sum(len(s.blockers) for s in sections)
    n_confirmed = sum(len(s.confirmed) for s in sections)
    lines = [
        f"📋 [Infra2] daily facet reconcile · {len(sections)} section(s) · "
        f"blockers {n_blockers} · confirmed {n_confirmed}",
    ]
    for s in sections:
        lines.append(f"\n── {s.name} ──")
        lines.append(s.report.strip())
    return "\n".join(lines)


def confirmed_findings(sections: list[Section]) -> list[str]:
    return [f"[{s.name}] {c}" for s in sections for c in s.confirmed]


def main() -> int:
    sections = run_all()
    report = combined_report(sections)
    print(report)

    from libs.alerting import deliver_infra2_report

    # Delivered EVERY run (#425): the report's own arrival self-proves the path.
    delivered = deliver_infra2_report(report)
    print(f"\n[report delivered: {delivered}]", file=sys.stderr)

    return 1 if any(s.blockers for s in sections) else 0


if __name__ == "__main__":
    sys.exit(main())
