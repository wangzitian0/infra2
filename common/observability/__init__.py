"""Infra2 Observability Domain Package."""

from __future__ import annotations

# PEP 562 lazy loading: SDK issue-trail helpers resolve straight from `infra2_sdk.observability`,
# while `probes` stays lazy so stdlib/lightweight scripts can import `common.observability`
# without forcing probe/postgres/s3 imports at package import time.
_LAZY: "dict[str, str]" = {
    "Breakdown": "common.observability.breakdown",
    "BreakdownVerdict": "common.observability.breakdown",
    "BreakdownWatch": "common.observability.watchers",
    "CheckVerdict": "infra2_sdk.observability",
    "ContainerBreakdownWatcher": "common.observability.watchers",
    "DEFAULT_TIMEOUT_SECONDS": "common.observability.probes",
    "GitHubIssues": "infra2_sdk.observability",
    "IssueApi": "infra2_sdk.observability",
    "MONTHLY_WINDOW_DAYS": "common.observability.digest",
    "OPENPANEL_CLIENTS": "common.observability.openpanel",
    "ProbeSpec": "common.observability.probes",
    "Trail": "infra2_sdk.observability",
    "analyze_container_logs": "common.observability.breakdown",
    "broken_state": "common.observability.breakdown",
    "build_breakdown_alert_payload": "common.observability.breakdown",
    "build_digest_card": "common.observability.digest",
    "build_digest_message": "common.observability.digest",
    "build_digest_text": "common.observability.digest",
    "classify_reason": "common.observability.breakdown",
    "collect_weekly_stability_section": "common.observability.digest",
    "deliver_digest": "common.observability.digest",
    "execute_probe": "common.observability.probes",
    "fetch_estate_app_health": "common.observability.digest",
    "find_breakdown_containers": "common.observability.breakdown",
    "load_trail": "infra2_sdk.observability",
    "openpanel_env": "common.observability.openpanel",
    "parse_probe_specs": "common.observability.probes",
    "reconcile_issue_trail": "infra2_sdk.observability",
    "record_verdicts": "infra2_sdk.observability",
    "run_digest_cli": "common.observability.digest",
    "run_probe": "common.observability.probes",
    "run_probes": "common.observability.probes",
    "summarize_watchdog_log_events": "common.observability.digest",
    "summarize_weekly_runs": "common.observability.digest",
    "sweep_breakdowns": "common.observability.watchers",
}

__all__ = sorted(_LAZY)


def __getattr__(name: str):
    from common.core.environ import resolve_lazy_attr

    return resolve_lazy_attr(name, _LAZY, globals(), __name__)


def __dir__() -> "list[str]":
    return sorted(set(globals()) | set(_LAZY))
