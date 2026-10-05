"""SigNoz notification channel and alert rule payloads."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from libs.alerting.types import (
    ERROR_LOG_LEVELS,
    SIGNOZ_ALERT_SCHEMA_VERSION,
    SIGNOZ_ALERT_VERSION,
    SIGNOZ_FEISHU_CHANNEL_PREFIX,
    AlertingError,
    BasicAuth,
    _dict,
    _required,
)


def signoz_feishu_channel_name(deploy_env: str | None = None) -> str:
    """Name of the SigNoz notification channel that targets the Feishu bridge.

    The single definition of that name. ``platform/12.alerting`` creates the channel
    under it and every rendered rule binds to it, because SigNoz v0.105 routes a rule
    to a channel **by name**: the dispatcher turns the threshold's ``channels`` entries
    into receiver names, and the channel name is the receiver name
    (``pkg/alertmanager/alertmanagerserver/dispatcher.go`` ``getOrCreateRoute``).
    A rule bound to anything else (a channel *id*, a typo) is accepted by the API and
    then fails at delivery with ``stage for receiver missing`` (#973).
    """
    return (
        f"{SIGNOZ_FEISHU_CHANNEL_PREFIX}-{(deploy_env or '').strip() or 'production'}"
    )


def signoz_rule_channels(channel_names: Iterable[str]) -> list[str]:
    """Normalise the channel names of a rule threshold; refuse to render none.

    SigNoz v0.105 builds a rule's route policy from these names when the rule is
    created and rejects a rule with none (``route_policy`` validation: "at least one
    channel is required"); a name that is no receiver is silently undeliverable. So an
    empty or non-string entry is an error here, not something to filter away.
    """
    if isinstance(channel_names, str):
        raise AlertingError(
            f"SigNoz rule channels must be a list of channel names, got {channel_names!r}"
        )
    channels: list[str] = []
    for name in channel_names:
        if not isinstance(name, str):
            raise AlertingError(
                f"SigNoz rule channels must be channel names (strings), got {name!r}"
            )
        name = name.strip()
        if name and name not in channels:
            channels.append(name)
    if not channels:
        raise AlertingError(
            "A SigNoz alert rule needs at least one notification channel NAME "
            "(condition.thresholds.spec[].channels); a rule without one never "
            "reaches anyone"
        )
    return channels


def signoz_rule_channel_problems(rule: Any, channel_name: str) -> list[str]:
    """Why ``rule`` (rendered, or read back from SigNoz) is not bound to ``channel_name``.

    Empty means bound. For a ``schemaVersion: v2alpha1`` rule SigNoz v0.105 reads the
    binding from ``condition.thresholds.spec[].channels`` (``BasicRuleThreshold.Channels``
    in ``pkg/types/ruletypes/threshold.go``) and only while
    ``notificationSettings.usePolicy`` is false; with ``usePolicy`` true the channels are
    ignored and the global routing policies decide. The legacy top-level
    ``preferredChannels`` becomes a threshold's channels only for ``schemaVersion: v1``
    rules (``processRuleDefaults``), so it is not written here.
    """
    if not isinstance(rule, Mapping):
        return ["rule is not an object"]
    problems: list[str] = []
    schema_version = rule.get("schemaVersion")
    if schema_version != SIGNOZ_ALERT_SCHEMA_VERSION:
        problems.append(
            f"schemaVersion is {schema_version!r}, not {SIGNOZ_ALERT_SCHEMA_VERSION!r}: "
            "channels are read from the thresholds only for that schema"
        )
    settings = rule.get("notificationSettings")
    if not isinstance(settings, Mapping) or settings.get("usePolicy"):
        problems.append(
            "notificationSettings.usePolicy must be false so the rule's own channels "
            "route it"
        )
    condition = _dict(rule.get("condition"))
    thresholds = _dict(condition.get("thresholds"))
    specs = thresholds.get("spec")
    if not isinstance(specs, list) or not specs:
        problems.append("condition.thresholds.spec has no threshold")
        return problems
    for index, spec in enumerate(specs):
        channels = _dict(spec).get("channels")
        if not isinstance(channels, list) or channel_name not in channels:
            problems.append(
                f"threshold {index} channels {channels!r} do not include "
                f"{channel_name!r}"
            )
    return problems


def build_signoz_channel_payload(
    *,
    channel_name: str,
    bridge_url: str,
    send_resolved: bool = True,
    basic_auth: BasicAuth | None = None,
) -> dict[str, Any]:
    """Build the SigNoz /api/v1/channels payload for the bridge webhook."""
    config: dict[str, Any] = {
        "send_resolved": bool(send_resolved),
        "url": bridge_url,
    }
    if basic_auth:
        config["http_config"] = {
            "basic_auth": {
                "username": basic_auth.username,
                "password": basic_auth.password,
            }
        }
    return {"name": channel_name, "webhook_configs": [config]}


def build_signoz_log_alert_rule_payload(
    *,
    alert_name: str,
    service_name: str,
    channel_names: list[str],
    summary: str,
    severity: str = "error",
    threshold: int = 0,
    match_type: str = "at_least_once",
    eval_window: str = "5m0s",
    frequency: str = "1m",
    service_id: str = "",
    environment: str = "production",
) -> dict[str, Any]:
    """Build a SigNoz v5 threshold rule counting a service's error-level OTEL logs.

    The query goes in the v5 ``compositeQuery.queries[]`` envelope. A rule marked
    ``version: v5`` is evaluated only from that list: SigNoz v0.105 copies
    ``queries`` into the query-range request and skips its v3→v5 migration for any
    rule already marked v5, so a v5 rule carrying the old ``builderQueries`` map runs
    no query at all and can never fire (#906).

    ``alertOnAbsent`` stays off: no error logs is the healthy state for this count.
    """
    from libs.core.service_identity import ServiceIdentity

    identity = ServiceIdentity.build(
        service_id or f"infra/{service_name}",
        environment,
        component=service_name,
        service_name=service_name,
    )
    log_filter = " AND ".join(
        (
            f"resource.service.name = {_signoz_filter_literal(service_name)}",
            "resource.deployment.environment.name = "
            f"{_signoz_filter_literal(identity.environment)}",
            "severity_text IN ["
            + ", ".join(_signoz_filter_literal(level) for level in ERROR_LOG_LEVELS)
            + "]",
        )
    )
    return {
        "alert": _required("alert_name", alert_name),
        "alertType": "LOGS_BASED_ALERT",
        "ruleType": "threshold_rule",
        "condition": {
            "thresholds": {
                "kind": "basic",
                "spec": [
                    {
                        "name": severity,
                        "target": int(threshold),
                        "matchType": _signoz_match_type(match_type),
                        "op": "1",
                        "channels": signoz_rule_channels(channel_names),
                        "targetUnit": "",
                    }
                ],
            },
            "compositeQuery": {
                "queryType": "builder",
                "panelType": "graph",
                "unit": "",
                "queries": [
                    {
                        "type": "builder_query",
                        "spec": {
                            "name": "A",
                            "signal": "logs",
                            "stepInterval": 60,
                            "aggregations": [{"expression": "count()"}],
                            "filter": {"expression": log_filter},
                            "disabled": False,
                        },
                    }
                ],
            },
            "selectedQueryName": "A",
            "alertOnAbsent": False,
            "requireMinPoints": False,
        },
        "evaluation": {
            "kind": "rolling",
            "spec": {"evalWindow": eval_window, "frequency": frequency},
        },
        "labels": {
            **identity.alert_labels(severity=severity),
            "team": "infra",
        },
        "annotations": {"description": summary, "summary": summary},
        "notificationSettings": {
            "groupBy": [],
            "renotify": {"enabled": False, "interval": "30m", "alertStates": []},
            "usePolicy": False,
        },
        "version": SIGNOZ_ALERT_VERSION,
        "schemaVersion": SIGNOZ_ALERT_SCHEMA_VERSION,
        "source": "infra2/platform/12.alerting",
        "disabled": False,
    }


def build_signoz_metric_alert_rule_payload(
    *,
    alert_name: str,
    promql: str,
    channel_names: list[str],
    summary: str,
    service_name: str = "finance-report-backend",
    severity: str = "warning",
    threshold: float = 0,
    threshold_unit: str = "",
    op: str = "above",
    match_type: str = "at_least_once",
    eval_window: str = "5m0s",
    frequency: str = "1m",
    group_by: list[str] | None = None,
    service_id: str = "",
    environment: str = "production",
) -> dict[str, Any]:
    """Build a SigNoz v5 PromQL rule for metric alerts.

    ``alertOnAbsent`` is always off here because SigNoz's PromQL rule never reads
    it (only the builder ``threshold_rule`` implements absence, v0.105 through
    v0.143). A PromQL rule that must fire on missing data says so in the query,
    with ``absent_over_time(...)``.
    """
    from libs.core.service_identity import ServiceIdentity

    identity = ServiceIdentity.build(
        service_id or f"infra/{service_name}",
        environment,
        component=service_name,
        service_name=service_name,
    )
    return {
        "alert": _required("alert_name", alert_name),
        "alertType": "METRIC_BASED_ALERT",
        "ruleType": "promql_rule",
        "condition": {
            "thresholds": {
                "kind": "basic",
                "spec": [
                    {
                        "name": severity,
                        "target": float(threshold),
                        "matchType": _signoz_match_type(match_type),
                        "op": _signoz_threshold_op(op),
                        "channels": signoz_rule_channels(channel_names),
                        "targetUnit": threshold_unit,
                    }
                ],
            },
            "compositeQuery": {
                "queryType": "promql",
                "panelType": "graph",
                "unit": threshold_unit,
                "queries": [
                    {
                        "type": "promql",
                        "spec": {
                            "name": "A",
                            "query": _required("promql", promql),
                            "legend": "",
                            "disabled": False,
                        },
                    }
                ],
            },
            "selectedQueryName": "A",
            "alertOnAbsent": False,
            "requireMinPoints": True,
        },
        "evaluation": {
            "kind": "rolling",
            "spec": {"evalWindow": eval_window, "frequency": frequency},
        },
        "labels": {
            **identity.alert_labels(severity=severity),
            "team": "infra",
        },
        "annotations": {"description": summary, "summary": summary},
        "notificationSettings": {
            "groupBy": group_by or [],
            "renotify": {"enabled": False, "interval": "30m", "alertStates": []},
            "usePolicy": False,
        },
        "version": SIGNOZ_ALERT_VERSION,
        "schemaVersion": SIGNOZ_ALERT_SCHEMA_VERSION,
        "source": "infra2/platform/12.alerting",
        "disabled": False,
    }


def find_signoz_channel_id(channels_response: Any, channel_name: str) -> str | None:
    """Find a SigNoz notification channel id by name across known response shapes."""
    for channel in _iter_signoz_items(channels_response, collection_keys=("channels",)):
        if not isinstance(channel, dict):
            continue
        if channel.get("name") != channel_name:
            continue
        channel_id = channel.get("id") or channel.get("channelId")
        return str(channel_id) if channel_id else None
    return None


def find_signoz_rule_id(rules_response: Any, alert_name: str) -> str | None:
    """Find a SigNoz rule id by alert name across known response shapes."""
    for rule in _iter_signoz_items(rules_response, collection_keys=("rules", "items")):
        if not isinstance(rule, dict):
            continue
        if rule.get("alert") != alert_name and rule.get("name") != alert_name:
            continue
        rule_id = rule.get("id") or rule.get("ruleId")
        return str(rule_id) if rule_id else None
    return None


def _signoz_filter_literal(value: str) -> str:
    """Quote a string for a SigNoz v5 filter expression (``'…'``, ``\\'`` escaped)."""
    escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _signoz_threshold_op(op: str) -> str:
    mapping = {
        "above": "1",
        "below": "2",
        "equal": "3",
        "not_equal": "4",
    }
    try:
        return mapping[op]
    except KeyError as exc:
        raise AlertingError(f"Unsupported SigNoz threshold op: {op!r}") from exc


def _signoz_match_type(match_type: str) -> str:
    mapping = {
        "at_least_once": "1",
        "all_times": "2",
        "all_the_times": "2",
        "on_average": "3",
        "in_total": "4",
        "last": "5",
    }
    try:
        return mapping[match_type]
    except KeyError as exc:
        raise AlertingError(f"Unsupported SigNoz match type: {match_type!r}") from exc


def _iter_signoz_items(
    response: Any, *, collection_keys: tuple[str, ...]
) -> list[dict[str, Any]]:
    if isinstance(response, list):
        return response
    if not isinstance(response, dict):
        return []

    data = response.get("data", response)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if any(key in data for key in ("name", "alert", "id", "channelId", "ruleId")):
            return [data]
        for key in collection_keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
        nested = data.get("data")
        if isinstance(nested, list):
            return nested
    return []
