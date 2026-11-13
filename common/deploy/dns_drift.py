#!/usr/bin/env python3
"""DNS drift check: intent versus external reality (read-only, a report, no gate).

Compares the DNS that the tooling INTENDS (bootstrap/02.dns_and_cert DEFAULT_RECORDS,
normalized) against what Cloudflare ACTUALLY serves. ``tools/facet_reconcile.py`` runs it
and delivers the report card; this module only observes and formats.

Config (env): CF_API_TOKEN, CF_ZONE_ID | CF_ZONE_NAME, INTERNAL_DOMAIN,
CF_RECORDS (optional comma list override of DEFAULT_RECORDS).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _dns_tasks():
    """Load bootstrap/02.dns_and_cert/tasks.py (dotted dir → importlib by path)."""
    spec = importlib.util.spec_from_file_location(
        "dns_cert_tasks", ROOT / "bootstrap/02.dns_and_cert/tasks.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class DnsDrift:
    missing: list[str]  # intended (expected) but ABSENT in Cloudflare — real drift
    unmanaged: list[str]  # present in Cloudflare but NOT in intent — informational

    @property
    def in_sync(self) -> bool:
        return not self.missing


def compute_drift(expected: set[str], actual: set[str]) -> DnsDrift:
    """Pure diff. `missing` (intent not realized) is the signal; `unmanaged` is info —
    other mechanisms (platform routing, manual) legitimately create records, so an extra
    record is reported, not treated as a failure."""
    return DnsDrift(
        missing=sorted(expected - actual), unmanaged=sorted(actual - expected)
    )


def format_report(drift: DnsDrift, domain: str, n_expected: int, n_actual: int) -> str:
    lines = [
        f"📋 [Infra2] DNS drift report · {domain}",
        f"intent {n_expected} records · cloudflare {n_actual} A/CNAME records",
    ]
    if drift.missing:
        lines.append(
            f"🔴 MISSING in Cloudflare ({len(drift.missing)}) — intent NOT realized:"
        )
        lines += [f"  · {r}" for r in drift.missing]
    if drift.unmanaged:
        lines.append(
            f"ℹ️ unmanaged ({len(drift.unmanaged)}) — in Cloudflare, not in DNS intent:"
        )
        lines += [f"  · {r}" for r in drift.unmanaged]
    if drift.in_sync and not drift.unmanaged:
        lines.append("✅ in sync — every intended record exists; nothing unmanaged.")
    elif drift.in_sync:
        lines.append(
            "✅ every intended record exists (unmanaged ones are informational)."
        )
    return "\n".join(lines)


def _expected_records(dns) -> list[str]:
    domain = os.environ["INTERNAL_DOMAIN"]
    # CF_RECORDS may arrive quoted (op stores it as "cloud,op,..."); strip wrapping quotes
    # before splitting, else the first/last name keep a stray quote and falsely look MISSING.
    override = os.environ.get("CF_RECORDS", "").strip().strip('"').strip("'")
    names = (
        [r.strip() for r in override.split(",") if r.strip()]
        if override
        else list(dns.DEFAULT_RECORDS)
    )
    return [dns._normalize_record(name, domain) for name in names]


def _actual_records(dns) -> list[str]:
    # Context-manage the client like the DNS tooling does, so the connection isn't leaked.
    with dns._cloudflare_client(os.environ["CF_API_TOKEN"]) as client:
        zone = dns._resolve_zone_id(
            client, os.environ.get("CF_ZONE_ID"), os.environ.get("CF_ZONE_NAME")
        )
        if not zone:
            raise RuntimeError(
                "could not resolve Cloudflare zone (CF_ZONE_ID / CF_ZONE_NAME)"
            )
        # one page (per_page=100) is enough for this zone; add pagination if it grows.
        result = dns._cf_request(
            client, "GET", f"/zones/{zone}/dns_records", params={"per_page": 100}
        )
    # The shared mutation-oriented Cloudflare helper returns ``None`` after it
    # has already rendered an API/transport error.  ``None`` is therefore an
    # observation failure, NOT an observed empty zone.  Treating it as ``[]``
    # turns a 401 into "every intended record is missing" and pages on invented
    # drift.
    if result is None:
        raise RuntimeError("Cloudflare DNS record observation failed")
    if not isinstance(result, list):
        raise RuntimeError(
            "Cloudflare DNS record observation returned a non-list result"
        )
    return [r["name"] for r in result if r.get("type") in ("A", "CNAME")]
