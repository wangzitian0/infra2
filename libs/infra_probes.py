"""Backward-compatibility shim — the implementation lives in `libs.observability.probes`.

Re-exports only. See `libs/README.md` § Backward-Compatibility Shims: never add new
business logic here; it belongs in the domain package this module points at.
"""

from __future__ import annotations

from libs.observability.probes import (
    DEFAULT_TIMEOUT_SECONDS,
    HTTP_PROBE_HEADERS,
    MISCONFIGURED_EXIT_CODE,
    PROBE_CLIENT_BLOCKED_MARKERS,
    ProbeMisconfigured,
    ProbeResult,
    ProbeSpec,
    build_probe_alert_payload,
    execute_probe,
    failed_results,
    group_severity,
    is_misconfigured,
    parse_probe_specs,
    post_alert_bridge_payload,
    run_probe,
    run_probes,
)

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "HTTP_PROBE_HEADERS",
    "MISCONFIGURED_EXIT_CODE",
    "PROBE_CLIENT_BLOCKED_MARKERS",
    "ProbeMisconfigured",
    "ProbeResult",
    "ProbeSpec",
    "build_probe_alert_payload",
    "execute_probe",
    "failed_results",
    "group_severity",
    "is_misconfigured",
    "parse_probe_specs",
    "post_alert_bridge_payload",
    "run_probe",
    "run_probes",
]
