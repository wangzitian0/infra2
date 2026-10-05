"""Card and text rendering for Feishu alerts and reports."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

import libs.alerting
from libs.alerting.types import (
    DEFAULT_ACTION,
    DEFAULT_IMPACT,
    DELIVERY_LABEL,
    FEISHU_BODY_LIMITS,
    FIELD_SEPARATOR,
    MAX_FIELD_CHARS,
    MAX_FULL_ITEMS,
    MAX_LOG_CHARS,
    MAX_LOG_LINES,
    MAX_MESSAGE_CHARS,
    MAX_SUMMARY_ITEMS,
    MAX_SUMMARY_LINE_CHARS,
    MAX_TITLE_NAME_CHARS,
    REPORT_DELIVERY,
    REPORT_TITLE_PREFIX,
    RUNBOOK_SECTION,
    _ACTION_BY_ALERT_OVERRIDE,
    _ACTION_BY_DOMAIN,
    _BODY_MARGIN,
    _IMPACT_BY_ALERT_OVERRIDE,
    _IMPACT_BY_DOMAIN,
    _LEVEL_RANK,
    _LEVEL_TEMPLATE,
    _RUNBOOK_BY_ALERT,
    _RUNBOOK_BY_ALERT_OVERRIDE,
    _RUNBOOK_BY_DOMAIN,
    PagerItem,
    PagerMessage,
    _clean,
    _clip,
    _dict,
    _epoch,
    _md_div,
    _one_line,
    _safe_url,
    _text_div,
    _truncate_message,
    firing_title,
    highest_level,
    pager_level,
    redact_secrets,
    since_text,
)

logger = logging.getLogger(__name__)

_CHAT_ID_PLACEHOLDER = "oc_" + "0" * 32


def _shrink_plans() -> Iterable[tuple[int, int, int]]:
    """(firing in full, recovered in full, summary lines per section), largest first.

    Fewer items are shown in full first; then the recovered items become summary
    lines; then the summary lines are cut from the end. The ✅ 已恢复 section is never
    what goes first. The Worker walks the same plans (worker.js ``SHRINK_PLANS``).
    """
    for full in range(MAX_FULL_ITEMS, 0, -1):
        yield full, full, MAX_SUMMARY_ITEMS
    for summaries in (MAX_SUMMARY_ITEMS, 10, 5, 3, 1, 0):
        yield 1, 0, summaries


def render_pager_text(message: PagerMessage) -> str:
    """Plain-text rendering (the Worker and the GitHub watchdog send text).

    Shrinks along ``_shrink_plans`` until the text fits ``MAX_MESSAGE_CHARS``.
    """
    text = ""
    for plan in _shrink_plans():
        text = _pager_text(message, *plan)
        if len(text) <= MAX_MESSAGE_CHARS:
            return text
    return _truncate_message(text)


def _pager_text(
    message: PagerMessage, full_firing: int, full_resolved: int, summaries: int
) -> str:
    lines = [message.title, *message.preamble]
    if message.report:
        lines += _summary_lines([*message.firing, *message.resolved], summaries)
        return "\n".join(lines)
    lines += _text_blocks(message.firing, full_firing, summaries)
    if message.resolved:
        if message.firing:
            lines += ["", f"✅ 已恢复 {len(message.resolved)} 项"]
        full = full_resolved if message.firing else full_firing
        lines += _text_blocks(message.resolved, full, summaries)
    return "\n".join(lines)


def _text_blocks(items: tuple[PagerItem, ...], full: int, summaries: int) -> list[str]:
    lines: list[str] = []
    for index, item in enumerate(items[:full], start=1):
        lines += ["", _item_heading(index, len(items), item)]
        for label, value in item.fields():
            if label == "日志":
                lines.append(f"{label}{FIELD_SEPARATOR}")
                lines += _log_lines(value)
            else:
                lines.append(f"{label}{FIELD_SEPARATOR}{_field_text(value)}")
    rest = list(items[full:])
    if rest:
        if full:
            lines += ["", f"另有 {len(rest)} 项,只列摘要:"]
        lines += _summary_lines(rest, summaries)
    return lines


def _summary_lines(items: list[PagerItem], limit: int = MAX_SUMMARY_ITEMS) -> list[str]:
    """One line per item, the first ``limit`` of them; the rest are counted."""
    lines = [
        f"• {item.summary_line()}" for item in items[: min(limit, MAX_SUMMARY_ITEMS)]
    ]
    hidden = len(items) - len(lines)
    if hidden > 0:
        lines.append(f"…及另外 {hidden} 项")
    return lines


def _item_heading(index: int, total: int, item: PagerItem) -> str:
    note = f" · {item.note}" if item.note else ""
    return f"— {index}/{total}{note} —"


def _field_text(value: str) -> str:
    return _clip(_one_line(_clean(value)), MAX_FIELD_CHARS)


def _log_lines(value: str) -> list[str]:
    lines = [
        _clip(line.strip(), MAX_FIELD_CHARS)
        for line in redact_secrets(_clean(value)).splitlines()
        if line.strip()
    ][-MAX_LOG_LINES:]
    kept: list[str] = []
    budget = MAX_LOG_CHARS
    for line in reversed(lines):  # keep the newest lines
        if len(line) + 1 > budget:
            break
        kept.insert(0, line)
        budget -= len(line) + 1
    return kept


def feishu_request_body(payload: dict[str, Any]) -> bytes:
    """The exact bytes of every Feishu request: UTF-8 JSON (#905).

    ASCII-escaping made one Chinese character cost 6 bytes; a webhook takes 20 KB.
    """
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def card_body_bytes(card: dict[str, Any], delivery_mode: str) -> int:
    """The size of the request body ``delivery_mode`` posts for ``card``."""
    if delivery_mode == "feishu_app":
        body = build_feishu_app_card_payload(_CHAT_ID_PLACEHOLDER, card)
    else:
        body = build_feishu_card_payload(card)
    return len(feishu_request_body(body))


def card_budget(delivery_mode: str) -> int:
    """Bytes a card may take in ``delivery_mode``; an unknown mode gets the smaller."""
    limit = FEISHU_BODY_LIMITS.get(delivery_mode, min(FEISHU_BODY_LIMITS.values()))
    return limit - _BODY_MARGIN


def render_pager_card(
    message: PagerMessage,
    *,
    template: str,
    external_url: str = "",
    delivery_mode: str = "feishu_webhook",
) -> dict[str, Any] | None:
    """Feishu interactive card: every field of every item is its own card field.

    Walks ``_shrink_plans`` until the body ``delivery_mode`` posts fits its budget;
    None when even the smallest plan does not (the caller sends the minimal card).
    """
    for plan in _shrink_plans():
        card = _pager_card(message, template, external_url, *plan)
        if card_body_bytes(card, delivery_mode) <= card_budget(delivery_mode):
            return card
    return None


def _pager_card(
    message: PagerMessage,
    template: str,
    external_url: str,
    full_firing: int,
    full_resolved: int,
    summaries: int,
) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    if message.preamble:
        elements.append(_text_div("\n".join(message.preamble)))
    if message.report:
        lines = _summary_lines([*message.firing, *message.resolved], summaries)
        if lines:
            elements.append(_text_div("\n".join(lines)))
    else:
        elements += _card_blocks(message.firing, full_firing, summaries)
        if message.resolved:
            full = full_resolved if message.firing else full_firing
            if message.firing:
                elements += [
                    {"tag": "hr"},
                    _md_div(f"**✅ 已恢复 {len(message.resolved)} 项**"),
                ]
            elements += _card_blocks(message.resolved, full, summaries)
    url = _safe_url(external_url)
    if url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "打开 SigNoz"},
                        "url": url,
                        "type": "primary",
                    }
                ],
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": message.title},
        },
        "elements": elements,
    }


def _card_blocks(
    items: tuple[PagerItem, ...], full: int, summaries: int
) -> list[dict[str, Any]]:
    elements: list[dict[str, Any]] = []
    for index, item in enumerate(items[:full], start=1):
        elements.append({"tag": "hr"})
        if len(items) > 1 or item.note:
            elements.append(_md_div(f"**{_item_heading(index, len(items), item)}**"))
        elements.append(
            {
                "tag": "div",
                "fields": [
                    {"is_short": False, "text": _card_field(label, value)}
                    for label, value in item.fields()
                ],
            }
        )
    rest = list(items[full:])
    if rest:
        elements.append({"tag": "hr"})
        if full:
            elements.append(_md_div(f"**另有 {len(rest)} 项,只列摘要:**"))
        elements.append(_text_div("\n".join(_summary_lines(rest, summaries))))
    return elements


def _card_field(label: str, value: str) -> dict[str, str]:
    """One card field. A value is plain text, never markup: whatever it holds (an
    ``<at>`` tag, a markdown link) is shown, not interpreted. Only our own runbook
    link is markup, and only after its URL passed ``_safe_url``."""
    if label == "Runbook":
        url = _safe_url(value)
        if url:
            name = re.sub(r"[\[\]()<>*`\\]", "", value.rsplit("/", 1)[-1])
            return {
                "tag": "lark_md",
                "content": f"**{label}**{FIELD_SEPARATOR}[{name}]({url})",
            }
    if label == "日志":
        text = "\n".join(_log_lines(value))
        return {"tag": "plain_text", "content": f"{label}{FIELD_SEPARATOR}\n{text}"}
    return {
        "tag": "plain_text",
        "content": f"{label}{FIELD_SEPARATOR}{_field_text(value)}",
    }


def pager_message_from_payload(
    payload: dict[str, Any], *, now: float | None = None
) -> PagerMessage:
    """Read an Alertmanager/SigNoz payload into the one pager layout.

    Per alert: ``labels`` (severity, environment, service_id, component,
    failure_domain, probe_kind), ``annotations`` (``symptom``, or a probe's
    ``target``/``expected``/``observed``, else ``summary`` with ``description``;
    optional ``container``/``compose``, ``impact``, ``next_step``, ``runbook_url``,
    ``log_tail``) and ``startsAt``/``endsAt``. A resolved alert names what recovered
    and how long it was down.
    """
    now = time.time() if now is None else now
    status = str(payload.get("status") or "unknown").lower()
    common_labels = _dict(payload.get("commonLabels"))
    common_annotations = _dict(payload.get("commonAnnotations"))
    alert_name = _clip(
        _one_line(
            _clean(
                common_labels.get("alertname")
                or _dict(payload.get("groupLabels")).get("alertname")
                or "SigNoz alert"
            )
        ),
        MAX_TITLE_NAME_CHARS,
    )
    raw_alerts = payload.get("alerts")
    alerts = [
        alert
        for alert in (raw_alerts if isinstance(raw_alerts, list) else [])
        if isinstance(alert, dict)
    ]
    if not alerts:  # say what the payload says, rather than an empty card
        alerts = [{"status": status, "labels": {}, "annotations": {}}]
    firing: list[PagerItem] = []
    resolved: list[PagerItem] = []
    for alert in alerts:
        is_resolved = str(alert.get("status") or status).lower() == "resolved"
        item = libs.alerting._payload_item(
            alert,
            resolved=is_resolved,
            common_labels=common_labels,
            common_annotations=common_annotations,
            alert_name=alert_name,
            now=now,
        )
        (resolved if is_resolved else firing).append(item)

    # the most severe first: they are the ones shown in full
    firing.sort(key=lambda item: _LEVEL_RANK[pager_level(item.level)])
    environments = sorted({item.environment for item in [*firing, *resolved]} - {""})
    where = (
        _clip(environments[0], 40)
        if len(environments) == 1
        else "多环境"
        if environments
        else ""
    )
    count = len(firing) or len(resolved)
    report = is_report_payload(payload)
    if report:
        title = f"{REPORT_TITLE_PREFIX}{alert_name}" + ("" if firing else " · 已恢复")
    elif firing:
        title = firing_title(highest_level(item.level for item in firing), alert_name)
    else:
        title = f"✅ [已恢复] {alert_name}"
    title += (f" · {where}" if where else "") + f" · {count} 项"
    return PagerMessage(
        title=title, firing=tuple(firing), resolved=tuple(resolved), report=report
    )


def _payload_item(
    alert: dict[str, Any],
    *,
    resolved: bool,
    common_labels: dict[str, Any],
    common_annotations: dict[str, Any],
    alert_name: str,
    now: float,
) -> PagerItem:
    labels = _dict(alert.get("labels"))
    annotations = _dict(alert.get("annotations"))
    severity = str(labels.get("severity") or common_labels.get("severity") or "")
    # `info` is what a resolved push carries (§3), not a level: say nothing then.
    level = (
        ""
        if resolved and severity.strip().lower() in {"", "info"}
        else pager_level(severity)
    )
    name = str(labels.get("alertname") or alert_name)
    start = _epoch(alert.get("startsAt"))
    end = (_epoch(alert.get("endsAt")) or now) if resolved else None
    item = PagerItem(
        level=level,
        environment=str(
            labels.get("environment") or common_labels.get("environment") or ""
        ),
        target=_object_text(labels, annotations, name),
        symptom=_symptom_text(labels, annotations, common_annotations),
        since=since_text(start, now=now, end=end),
    )
    if resolved:
        return item
    domain = str(labels.get("failure_domain") or "")
    impact = str(
        annotations.get("impact")
        or _IMPACT_BY_ALERT_OVERRIDE.get(name)
        or _IMPACT_BY_DOMAIN.get(domain)
        or DEFAULT_IMPACT
    )
    return replace(
        item,
        impact=f"[{domain}] {impact}" if domain else impact,
        action=str(
            annotations.get("next_step")
            or _ACTION_BY_ALERT_OVERRIDE.get(name)
            or _ACTION_BY_DOMAIN.get(domain)
            or DEFAULT_ACTION
        ),
        runbook=_runbook_url(name, domain, annotations),
        log=str(annotations.get("log_tail") or ""),
    )


def _object_text(labels: dict[str, Any], annotations: dict[str, Any], name: str) -> str:
    """对象: the full service_id plus the probe / container / check name."""
    what = (
        annotations.get("container")
        or annotations.get("compose")
        or labels.get("component")
        or labels.get("stream")
        or labels.get("instance")
        or labels.get("service")
        or labels.get("job")
    )
    parts = [str(part) for part in (labels.get("service_id"), what) if part]
    return " · ".join(parts) or name


def _symptom_text(
    labels: dict[str, Any],
    annotations: dict[str, Any],
    common_annotations: dict[str, Any],
) -> str:
    """现象: what the source says went wrong, with the evidence behind it.

    A source-written ``symptom`` is shown as it is. A probe shows its target, what
    was expected and what was observed. Anything else shows its ``summary`` with its
    ``description``. Evidence is redacted (``redact_secrets``).
    """
    if annotations.get("symptom"):
        return str(annotations["symptom"])
    description = str(annotations.get("description") or "")
    if "target" in annotations or "expected" in annotations:
        head = " ".join(
            str(part)
            for part in (labels.get("probe_kind"), annotations.get("target"))
            if part
        )
        body = "; ".join(
            part
            for part in (
                f"期望 {annotations['expected']}"
                if annotations.get("expected")
                else "",
                f"实际 {annotations['observed']}"
                if annotations.get("observed")
                else "",
            )
            if part
        )
        # the runner's own "expected X, observed Y" / "probe passed" add nothing
        if description and not description.startswith(("expected ", "probe passed")):
            body = f"{body}; {description}" if body else description
        return redact_secrets(f"{head} → {body}" if head and body else head or body)
    summary = str(
        annotations.get("summary")
        or common_annotations.get("summary")
        or common_annotations.get("info")
        or ""
    )
    if summary and description and description not in summary:
        return redact_secrets(f"{summary} — {description}")
    return redact_secrets(summary or description)


def _runbook_url(name: str, domain: str, annotations: dict[str, Any]) -> str:
    return (
        _safe_url(annotations.get("runbook_url"))
        and str(annotations["runbook_url"]).strip()
        or _RUNBOOK_BY_ALERT_OVERRIDE.get(name)
        or _RUNBOOK_BY_DOMAIN.get(domain)
        or _RUNBOOK_BY_ALERT.get(name)
        or RUNBOOK_SECTION
    )


def build_feishu_alert_card(
    payload: dict[str, Any],
    *,
    now: float | None = None,
    delivery_mode: str = "feishu_webhook",
) -> dict[str, Any]:
    """Render a SigNoz/Alertmanager payload as a Feishu interactive card (#905).

    A page: header coloured by level (P0 red, P1 orange, P2 yellow; green once
    resolved), one block of card fields per alert in ``PAGER_FIELDS`` order, the
    first ``MAX_FULL_ITEMS`` in full and the rest one line each, within the body
    budget of ``delivery_mode``. A report (``delivery=report``): blue, titled
    ``[报告]``, one line per item.

    Never raises and never drops the page: a payload the renderer cannot handle, or a
    card that cannot fit, is sent as ``minimal_alert_card`` and the reason is logged.
    """
    try:
        message = libs.alerting.pager_message_from_payload(payload, now=now)
        if message.report:
            template = "blue"
        elif message.firing:
            template = _LEVEL_TEMPLATE[
                highest_level(item.level for item in message.firing)
            ]
        else:
            template = "green"
        external_url = payload.get("externalURL")
        card = render_pager_card(
            message,
            template=template,
            external_url=external_url if isinstance(external_url, str) else "",
            delivery_mode=delivery_mode,
        )
    except Exception as exc:  # noqa: BLE001 - a page must survive its own rendering
        logger.exception("alert card render failed; sending the minimal card")
        return minimal_alert_card(
            payload,
            reason=f"渲染失败 {type(exc).__name__}",
            delivery_mode=delivery_mode,
        )
    if card is None:
        logger.warning(
            "alert card over the %s body budget; sending the minimal card",
            delivery_mode,
        )
        return minimal_alert_card(
            payload, reason="完整卡片超出大小上限", delivery_mode=delivery_mode
        )
    return card


def minimal_alert_card(
    payload: object, *, reason: str, delivery_mode: str = "feishu_webhook"
) -> dict[str, Any]:
    """The last-resort card: per alert its level, environment, object and raw summary,
    as plain text, inside the body budget of ``delivery_mode``. It reads the payload
    defensively and falls back to a fixed card rather than raise."""
    try:
        data = payload if isinstance(payload, dict) else {}
        common = _dict(data.get("commonLabels"))
        common_annotations = _dict(data.get("commonAnnotations"))
        name = _clip(
            _one_line(_clean(common.get("alertname") or "告警")), MAX_TITLE_NAME_CHARS
        )
        raw_alerts = data.get("alerts")
        alerts = (
            [a for a in raw_alerts if isinstance(a, dict)]
            if isinstance(raw_alerts, list)
            else []
        )
        lines = []
        for alert in alerts or [{}]:
            labels = _dict(alert.get("labels"))
            annotations = _dict(alert.get("annotations"))
            what = labels.get("component") or labels.get("instance") or ""
            parts = (
                pager_level(labels.get("severity") or common.get("severity")),
                str(labels.get("environment") or common.get("environment") or ""),
                " · ".join(str(p) for p in (labels.get("service_id"), what) if p)
                or name,
                str(
                    annotations.get("summary")
                    or annotations.get("description")
                    or common_annotations.get("summary")
                    or ""
                ),
            )
            line = " · ".join(_one_line(_clean(part)) for part in parts if part)
            lines.append("• " + _clip(redact_secrets(line), MAX_SUMMARY_LINE_CHARS))
        level = highest_level(
            _dict(alert.get("labels")).get("severity") or common.get("severity")
            for alert in (alerts or [{}])
        )
        resolved = str(data.get("status") or "").lower() == "resolved"
        title = (
            f"✅ [已恢复] {name}" if resolved else firing_title(level, name)
        ) + " · 简化卡片"
        if is_report_payload(data):
            title = f"{REPORT_TITLE_PREFIX}{name} · 简化卡片"
        shown = lines[:MAX_SUMMARY_ITEMS]
        while True:
            body = list(shown)
            if len(lines) > len(shown):
                body.append(f"…及另外 {len(lines) - len(shown)} 项")
            card = {
                "config": {"wide_screen_mode": True},
                "header": {
                    "template": "green" if resolved else "red",
                    "title": {"tag": "plain_text", "content": title},
                },
                "elements": [
                    _text_div("\n".join(body) or "(无告警明细)"),
                    _text_div(f"完整卡片未能生成:{reason};详情见 bridge 日志"),
                ],
            }
            if not shown or card_body_bytes(card, delivery_mode) <= card_budget(
                delivery_mode
            ):
                return card
            shown = shown[:-1]
    except Exception:  # noqa: BLE001 - the last resort must not raise
        logger.exception("minimal alert card failed; sending the fixed card")
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "red",
                "title": {"tag": "plain_text", "content": "🚨 告警(卡片生成失败)"},
            },
            "elements": [
                _text_div(f"完整卡片与简化卡片都未能生成:{reason};详情见 bridge 日志")
            ],
        }


def format_signoz_alert(payload: dict[str, Any], *, now: float | None = None) -> str:
    """The same payload as ``build_feishu_alert_card``, rendered as text."""
    return render_pager_text(libs.alerting.pager_message_from_payload(payload, now=now))


def mark_report_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of an Alertmanager-shaped payload labelled ``delivery=report``.

    The label goes on ``commonLabels`` (what the bridge routes on) and on every alert,
    because Alertmanager's commonLabels are by definition the labels all alerts share.
    """
    marked = dict(payload)
    marked["commonLabels"] = {
        **_dict(payload.get("commonLabels")),
        DELIVERY_LABEL: REPORT_DELIVERY,
    }
    alerts = payload.get("alerts")
    if isinstance(alerts, list):
        marked["alerts"] = [
            {
                **alert,
                "labels": {
                    **_dict(alert.get("labels")),
                    DELIVERY_LABEL: REPORT_DELIVERY,
                },
            }
            if isinstance(alert, dict)
            else alert
            for alert in alerts
        ]
    return marked


def is_report_payload(payload: dict[str, Any]) -> bool:
    """True when the payload asks to be delivered as a report (``delivery=report``)."""
    label = _dict(payload.get("commonLabels")).get(DELIVERY_LABEL, "")
    return str(label).strip().lower() == REPORT_DELIVERY


def build_feishu_card_payload(card: dict[str, Any]) -> dict[str, Any]:
    """Build a Feishu custom-bot interactive-card payload."""
    return {"msg_type": "interactive", "card": card}


def build_feishu_app_card_payload(chat_id: str, card: dict[str, Any]) -> dict[str, Any]:
    """Build a Feishu OpenAPI interactive-card message payload."""
    return {
        "receive_id": chat_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
    }


def build_feishu_text_payload(text: str) -> dict[str, Any]:
    """Build a Feishu custom bot text payload."""
    message = text.strip() or "SigNoz alert"
    if len(message) > MAX_MESSAGE_CHARS:
        message = _truncate_message(message)
    return {"msg_type": "text", "content": {"text": message}}


def build_feishu_app_message_payload(chat_id: str, text: str) -> dict[str, Any]:
    """Build a Feishu OpenAPI text message payload."""
    message = text.strip() or "SigNoz alert"
    if len(message) > MAX_MESSAGE_CHARS:
        message = _truncate_message(message)
    return {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": message}, ensure_ascii=False),
    }
