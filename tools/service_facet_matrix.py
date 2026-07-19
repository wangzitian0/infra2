#!/usr/bin/env python3
"""Service × facet completeness matrix (#541).

Extends the validate-deployers gate: for every registry service (a deploy.py
Deployer subclass) and every facet column ({probes, signals, backups}), the cell
is one of:

- ``declared``  — the Deployer declares the facet;
- ``exempt``    — the Deployer carries an explicit ``Exemption(check_id=..., reason=...)``;
- ``MISSING``   — neither: an undeclared cell, i.e. the convergence backlog.

Cross-facet consistency flags surface declared-but-inconsistent combinations —
today the Infra-012.10 scenario this repo shipped twice (#531/#475) and never
enforced: a service declaring a *critical* ProbeFacet with NO SignalFacet
(tier/debounce) and no ``signals`` exemption.

Report-only by default (the repo's established ratchet — mirror of
tools/ci_gate_audit.py Phase 1): the full backlog is printed but the exit code
stays 0. ``--enforce`` flips it fail-closed once the backlog is cleared.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.service_registry import ServiceMeta, service_attrs  # noqa: E402

FACET_COLUMNS = ("probes", "signals", "backups")
MISSING = "MISSING"


def cell_state(meta: ServiceMeta, column: str) -> str:
    """One matrix cell: declared / exempt / MISSING."""
    if getattr(meta, column):
        return "declared"
    if meta.exempted(column):
        return "exempt"
    return MISSING


def consistency_flags(attrs: dict[str, ServiceMeta]) -> list[str]:
    """Declared-but-inconsistent facet combinations (each line is one finding)."""
    flags: list[str] = []
    for service_id in sorted(attrs):
        meta = attrs[service_id]
        # Infra-012.10 counterfactual: a critical probe means this service can
        # page — paging without a declared signal tier/debounce is exactly the
        # un-debounced-flapping bug class (#475/#531). Exempting `signals`
        # requires writing down why.
        has_critical_probe = any(p.severity == "critical" for p in meta.probes)
        if has_critical_probe and not meta.signals and not meta.exempted("signals"):
            flags.append(
                f"{service_id}: declares critical ProbeFacet(s) but no SignalFacet "
                "(tier/type/consecutive_failures/renotify_window_sec) and no "
                "'signals' exemption — the Infra-012.10 gap (#531/#475)"
            )
        # An exemption for a facet that IS declared is a stale contradiction.
        for column in FACET_COLUMNS:
            if getattr(meta, column) and meta.exempted(column):
                flags.append(
                    f"{service_id}: declares `{column}` AND carries a '{column}' "
                    "exemption — remove the stale exemption"
                )
    return flags


def build_matrix(attrs: dict[str, ServiceMeta]) -> list[tuple[str, dict[str, str]]]:
    """Sorted (service_id, {column: state}) rows for every registry service."""
    return [
        (service_id, {col: cell_state(attrs[service_id], col) for col in FACET_COLUMNS})
        for service_id in sorted(attrs)
    ]


def render_report(attrs: dict[str, ServiceMeta]) -> tuple[str, bool]:
    """(report text, clean) — clean=False when any MISSING cell or flag exists."""
    rows = build_matrix(attrs)
    flags = consistency_flags(attrs)

    id_width = max(len("service"), *(len(sid) for sid, _ in rows))
    col_width = max(len(MISSING), *(len(c) for c in FACET_COLUMNS))
    lines = ["service × facet completeness matrix (#541):", ""]
    header = "  ".join(
        [f"{'service':<{id_width}}"] + [f"{c:<{col_width}}" for c in FACET_COLUMNS]
    )
    lines.append(header)
    lines.append("-" * len(header))
    missing_cells = 0
    for service_id, cells in rows:
        missing_cells += sum(1 for state in cells.values() if state == MISSING)
        lines.append(
            "  ".join(
                [f"{service_id:<{id_width}}"]
                + [f"{cells[c]:<{col_width}}" for c in FACET_COLUMNS]
            )
        )
    lines.append("")
    lines.append(
        f"{len(rows)} services × {len(FACET_COLUMNS)} facets: "
        f"{missing_cells} MISSING cell(s) (the undeclared-facet backlog)"
    )
    if flags:
        lines.append("")
        lines.append(f"{len(flags)} cross-facet consistency flag(s):")
        lines.extend(f"  - {flag}" for flag in flags)
    return "\n".join(lines), not (missing_cells or flags)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="exit non-zero on any MISSING cell or consistency flag "
        "(default: report-only ratchet)",
    )
    args = parser.parse_args(argv)

    report, clean = render_report(service_attrs())
    print(report)
    if args.enforce and not clean:
        print(
            "::error::service facet matrix has MISSING cells or consistency flags",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
