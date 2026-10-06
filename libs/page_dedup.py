"""Backward-compatibility shim — the implementation lives in `libs.observability.page_dedup`.

Re-exports only. See `libs/README.md` § Backward-Compatibility Shims: never add new
business logic here; it belongs in the domain package this module points at.
"""

from __future__ import annotations

from libs.observability.page_dedup import (
    ABSENT,
    ALL_UNEVALUATED,
    CORRUPT,
    Decision,
    Finding,
    FOUND,
    Identity,
    Lookup,
    MAX_SHOWN_KEY_CHARS,
    NONE,
    Outcome,
    PAGE,
    REPORT,
    RESOLVED,
    State,
    StateApi,
    UNREADABLE,
    day_number,
    decide,
    dedup_page,
    encode_marker,
    find_state,
    fingerprint,
    make_identity,
    parse_marker,
    resolve_page_state,
    run_url_from_env,
)

__all__ = [
    "ABSENT",
    "ALL_UNEVALUATED",
    "CORRUPT",
    "Decision",
    "Finding",
    "FOUND",
    "Identity",
    "Lookup",
    "MAX_SHOWN_KEY_CHARS",
    "NONE",
    "Outcome",
    "PAGE",
    "REPORT",
    "RESOLVED",
    "State",
    "StateApi",
    "UNREADABLE",
    "day_number",
    "decide",
    "dedup_page",
    "encode_marker",
    "find_state",
    "fingerprint",
    "make_identity",
    "parse_marker",
    "resolve_page_state",
    "run_url_from_env",
]
