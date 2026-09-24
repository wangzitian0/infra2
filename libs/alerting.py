"""Alerting helpers for SigNoz to Feishu delivery."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

FEISHU_WEBHOOK_HOSTS = {"open.feishu.cn", "open.larksuite.com"}
FEISHU_WEBHOOK_PATH_PREFIX = "/open-apis/bot/v2/hook/"
SIGNOZ_ALERT_SCHEMA_VERSION = "v2alpha1"
SIGNOZ_ALERT_VERSION = "v5"
# Log severity texts an error-log rule counts. Python's OTel LoggingHandler sends a
# CRITICAL record as FATAL in current SDK releases and as CRITICAL in older ones, so
# both spellings are counted.
ERROR_LOG_LEVELS = ("ERROR", "CRITICAL", "FATAL")
MAX_MESSAGE_CHARS = 3500
JSON_UTF8 = "application/json; charset=utf-8"
TRUNCATION_SUFFIX = "\n...[truncated]"
# #903: a payload labelled `delivery=report` is a REPORT, not a page. The bridge sends it
# to FEISHU_REPORT_CHAT_ID (app mode) or, when that is unset, to the pager chat. Either
# way its title starts with REPORT_TITLE_PREFIX (#905), so nobody mistakes it for a page.
DELIVERY_LABEL = "delivery"
REPORT_DELIVERY = "report"
REPORT_TITLE_PREFIX = "[报告] "

# ---------------------------------------------------------------------------
# #905: one layout for every pager message (ops.observability.md §3.1).
#
# The bridge card, the Cloudflare Worker's text and the GitHub watchdog's text all
# show these fields, in this order, under these labels (cloudflare/infra-watchdog
# /worker.js keeps a copy; libs/tests/test_cloudflare_watchdog.py holds it to this
# one). Labels and the prose the sources write are Chinese; commands, paths and
# identifiers stay verbatim; evidence is shown as captured.
PAGER_FIELDS = (
    "级别",
    "环境",
    "对象",
    "现象",
    "开始于",
    "影响",
    "下一步",
    "Runbook",
    "日志",
)
FIELD_SEPARATOR = "："
#: Times are shown in the owner's zone; durations are zone-free.
DISPLAY_TIMEZONE = timezone(timedelta(hours=8))
DISPLAY_TIMEZONE_LABEL = "UTC+8"
#: The first N items of a message are shown in full; the rest one line each.
MAX_FULL_ITEMS = 5
MAX_SUMMARY_ITEMS = 20
MAX_FIELD_CHARS = 300
MAX_SUMMARY_CHARS = 160
#: one summary line, all of it
MAX_SUMMARY_LINE_CHARS = 240
MAX_TITLE_NAME_CHARS = 80
MAX_URL_CHARS = 500
MAX_LOG_CHARS = 800
MAX_LOG_LINES = 8
#: What Feishu accepts per request body, by delivery mode: a custom-bot webhook 20 KB,
#: an app-bot card message 30 KB. A card is budgeted against the mode that sends it,
#: measured on the exact bytes that mode posts (``feishu_request_body``).
FEISHU_BODY_LIMITS = {"feishu_webhook": 20_000, "feishu_app": 30_000}
_BODY_MARGIN = 1_024
#: Epoch seconds a pager time may carry; anything else is an unknown time.
_EARLIEST_EPOCH = 1_000_000_000  # 2001-09-09
_LATEST_EPOCH = 4_102_444_800  # 2100-01-01

# ops.observability.md §3: critical = P0, error = P1, warning = P2. An unknown value
# counts as critical, so a typo pages loud instead of quiet.
_LEVEL_BY_SEVERITY = {
    "critical": "P0",
    "error": "P1",
    "warning": "P2",
    "p0": "P0",
    "p1": "P1",
    "p2": "P2",
}
_LEVEL_RANK = {"P0": 0, "P1": 1, "P2": 2}
_LEVEL_EMOJI = {"P0": "🔴", "P1": "🟠", "P2": "🟡"}
_LEVEL_TEMPLATE = {"P0": "red", "P1": "orange", "P2": "yellow"}

_REPO_BLOB = "https://github.com/wangzitian0/infra2/blob/main"
_P0_RUNBOOK_BASE = f"{_REPO_BLOB}/docs/runbooks/infra022-p0.md"
_ALERTING_README = f"{_REPO_BLOB}/platform/12.alerting/README.md"
#: The runbook section every alert without a more specific anchor links to.
RUNBOOK_SECTION = (
    f"{_REPO_BLOB}/docs/ssot/ops.observability.md#7-标准操作程序-playbooks"
)
# Lookup order for 影响 / 下一步 / Runbook: an alertname whose meaning overrides the
# failure domain (a probe in the misconfigured lane, a daily digest), then the failure
# domain, then the alertname in general.
_RUNBOOK_BY_ALERT_OVERRIDE = {
    "InfraProbeMisconfigured": f"{_ALERTING_README}#failing-round-trips-two-lanes-726",
    "InfraProbeChronic": f"{_ALERTING_README}#infra-service-probes",
    "ContainerBreakdownChronic": f"{_P0_RUNBOOK_BASE}#container-killed",
}
_RUNBOOK_BY_DOMAIN = {
    "runtime": f"{_P0_RUNBOOK_BASE}#container-killed",
    "host-memory": f"{_P0_RUNBOOK_BASE}#container-killed",
    "deploy-queue": f"{_P0_RUNBOOK_BASE}#deployment-failed",
    "probe-client-blocked": f"{_ALERTING_README}#public-route-probes",
    "host-disk": f"{_P0_RUNBOOK_BASE}#disk-full",
    "host-mem": f"{_ALERTING_README}#host-resource-probes",
    "host-cpu": f"{_ALERTING_README}#host-resource-probes",
    "backup": f"{_REPO_BLOB}/docs/ssot/ops.recovery.md#sop-004-备份-freshness-验证",
}
_RUNBOOK_BY_ALERT = {
    "ContainerBreakdown": f"{_P0_RUNBOOK_BASE}#container-killed",
    "DeployQueueStuck": f"{_P0_RUNBOOK_BASE}#deployment-failed",
    "InfraServiceProbeFailed": f"{_ALERTING_README}#infra-service-probes",
    "InfraPublicRouteProbeFailed": f"{_ALERTING_README}#public-route-probes",
}
_IMPACT_BY_ALERT_OVERRIDE = {
    "InfraProbeMisconfigured": "探针没有测到目标(自身配置缺失,或仍在宽限期内):目标是否健康未知",
    "InfraProbeChronic": "故障已持续超过一天;每日摘要,不再呼人",
    "ContainerBreakdownChronic": "容器反复坏或一直坏;摘要,同一故障不再单独呼人",
}
_IMPACT_BY_DOMAIN = {
    "service-or-route": "该服务或它的路由不可用:依赖它的请求失败",
    "probe-client-blocked": "边缘拒绝了探针(error 1010):服务可能正常,但这条路由暂时失去监控",
    "runtime": "容器崩溃循环、退出或不健康:它承载的服务降级或不可用",
    "host-memory": "宿主机内存耗尽:同机其他容器也可能被 OOM 终止",
    "deploy-queue": "单并发 FIFO 部署队列被占住:之后的部署全部排队",
    "host-disk": "宿主机磁盘将满:Docker 写入、数据库提交与备份都可能失败",
    "host-mem": "宿主机内存吃紧:再涨就会有容器被 OOM 终止",
    "host-cpu": "宿主机 CPU 持续过高:所有服务变慢,探测与告警也可能超时",
    "backup": "异地备份缺失或过期:数据在下次成功备份前没有保护",
}
DEFAULT_IMPACT = "未声明 failure_domain:按现象判断影响范围"
_ACTION_BY_ALERT_OVERRIDE = {
    "InfraProbeMisconfigured": "核对该探针自己的配置(round-trip 的客户端 id、URL 等);目标是否健康另行确认",
    "InfraProbeChronic": "确认有人在处理;修好它,或确认它不该再被探测",
    "ContainerBreakdownChronic": "按 runbook 找根因,不要只重启",
}
_ACTION_BY_DOMAIN = {
    "service-or-route": (
        "在 VPS 上对目标复现(http:`curl -sS -m 10 -o /dev/null -w '%{http_code}'`),"
        "再看该服务容器的 `docker logs --tail 80`,对照最近一次部署"
    ),
    "probe-client-blocked": (
        "在 Cloudflare 安全事件里找拦下探针的规则(error 1010)并放行探针 User-Agent;"
        "服务本身用浏览器另行确认"
    ),
    "runtime": (
        "先留证据再动手:对该容器执行 `docker inspect -f '{{.State.Status}} "
        "{{.State.ExitCode}} {{.State.OOMKilled}}'` 与 `docker logs --tail 80`"
    ),
    "host-memory": "用 `free -h` 与 `docker stats --no-stream` 找出内存大户,先处理宿主机内存",
    "deploy-queue": (
        "对照 Dokploy 部署记录与 CI 运行记录;只用 Dokploy 自己的 `cancel` / `clean`"
        "(`DEPLOY_GUARD_REMEDIATE=1`),绝不直接删 Redis / BullMQ 键"
    ),
    "host-disk": (
        "按 runbook 找增长来源:`du -xhd1 /data | sort -h` 与 `docker system df`,"
        "再看 `systemctl status infra2-disk-guardian.timer --no-pager`;"
        "不要删卷、在用的镜像或备份"
    ),
    "host-mem": "用 `free -h` 与 `docker stats --no-stream` 找出内存大户",
    "host-cpu": "用 `top -o %CPU` 与 `docker stats --no-stream` 找出 CPU 大户(dockerd 忙循环是已知一类)",
    "backup": (
        "在宿主机上查 `/var/log/infra2-backup*.log` 的失败行,按 `ops.recovery.md` "
        "SOP-006 重跑备份,再按 SOP-004 校验 manifest"
    ),
}
DEFAULT_ACTION = "按现象排查;SigNoz 规则可在 SigNoz 打开,看告警时段的指标与日志"
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# What a log tail or a piece of evidence must not carry to Feishu (#905): credentials
# in a DSN, bearer tokens, a custom-bot hook, and the value given to anything named
# secret/token/password/api key. The names stay: `VAULT_SECRET_ID are required` is
# the evidence a breakdown card exists to show.
_CREDENTIAL_REDACTIONS = (
    (re.compile(r"([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^@\s/]+@", re.I), r"\1***:***@"),
    (re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]+", re.I), "Bearer ***"),
    (
        re.compile(
            r"https://open\.(?:feishu\.cn|larksuite\.com)/open-apis/bot/v2/hook/[^\s]+"
        ),
        "https://open.feishu.cn/open-apis/bot/v2/hook/***",
    ),
)
_SECRET_VALUE = re.compile(
    r"(?i)\b([\w.-]*(?:secret|token|password|passwd|api[_-]?key)[\w.-]*)"
    r"(\s*[=:]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
logger = logging.getLogger("alert-bridge")
# Environments with nobody on call: staging and the preview slots. An allowlist on
# purpose — "prod", a typo or an unset value is not on it, so it pages (#903 review).
REPORT_ONLY_ENVIRONMENTS = frozenset({"staging", "preview"})


def is_report_only_environment(value: str | None) -> bool:
    """True only for an environment known to have no pager: staging or a preview slot.

    The value is normalized first (``libs.common.normalize_env_name``: ``stg`` is
    staging, ``PRODUCTION `` and unset are production). Preview slots are ``preview``
    or ``<kind>-<value>`` for the preview kinds (``pr-5``, ``branch-main``, ...).
    Anything else — production, an unknown name, garbage — pages: an environment
    that cannot be recognised must fail loud, not quiet.
    """
    from libs.common import normalize_env_name
    from libs.deploy_env_config import PREVIEW_KINDS

    raw = (value or "").strip().lower().replace("-", "_")
    try:
        name = normalize_env_name(raw)
    except ValueError:  # e.g. a "/" in it: not an environment this estate names
        return False
    if name in REPORT_ONLY_ENVIRONMENTS:
        return True
    return any(name.startswith(f"{kind}_") for kind in (*PREVIEW_KINDS, "preview"))


class AlertingError(Exception):
    """Base alerting error."""


class InvalidWebhookUrl(AlertingError):
    """Raised when a Feishu webhook URL is unsafe or unsupported."""


class InvalidFeishuAppConfig(AlertingError):
    """Raised when Feishu app delivery config is incomplete or unsafe."""


class FeishuDeliveryError(AlertingError):
    """Raised when Feishu webhook delivery fails."""


@dataclass(frozen=True)
class BasicAuth:
    username: str
    password: str


def validate_feishu_webhook_url(url: str) -> str:
    """Validate and return a Feishu/Lark custom bot webhook URL."""
    candidate = (url or "").strip()
    parsed = urlparse(candidate)
    if parsed.scheme != "https":
        raise InvalidWebhookUrl("Feishu webhook URL must use https")
    if parsed.hostname not in FEISHU_WEBHOOK_HOSTS:
        allowed = ", ".join(sorted(FEISHU_WEBHOOK_HOSTS))
        raise InvalidWebhookUrl(f"Feishu webhook host must be one of: {allowed}")
    if not parsed.path.startswith(FEISHU_WEBHOOK_PATH_PREFIX):
        raise InvalidWebhookUrl("Feishu webhook path must be a custom bot hook")
    token = parsed.path[len(FEISHU_WEBHOOK_PATH_PREFIX) :]
    if not token or "/" in token:
        raise InvalidWebhookUrl("Feishu webhook token must be a non-empty path segment")
    return candidate


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


def pager_level(severity: object) -> str:
    """P0/P1/P2 for a severity label (ops.observability.md §3).

    ``critical`` is P0, ``error`` P1, ``warning`` P2; P0/P1/P2 pass through. Anything
    else is P0: §3 counts an unknown severity as critical, so a typo pages loud.
    """
    return _LEVEL_BY_SEVERITY.get(str(severity or "").strip().lower(), "P0")


def highest_level(levels: Iterable[str]) -> str:
    """The most severe of ``levels`` (P0 before P1 before P2); P0 when there are none."""
    return min(
        (pager_level(level) for level in levels),
        key=_LEVEL_RANK.__getitem__,
        default="P0",
    )


def firing_title(level: str, subject: str) -> str:
    """The title of a page: ``🔴 [P0 告警] <subject>`` (#905)."""
    level = pager_level(level)
    return f"{_LEVEL_EMOJI[level]} [{level} 告警] {subject}"


def format_time(epoch: float) -> str:
    """``2026-09-24 16:00（UTC+8）`` -- the one time format of every pager message.

    The owner's zone (UTC+8, no daylight saving) is the display; payloads keep UTC.
    """
    shown = datetime.fromtimestamp(epoch, tz=DISPLAY_TIMEZONE)
    return shown.strftime("%Y-%m-%d %H:%M") + f"（{DISPLAY_TIMEZONE_LABEL}）"


def format_duration(seconds: float) -> str:
    """A human duration in Chinese: ``3 天 2 小时``, ``1 小时 5 分钟``, ``12 分钟``."""
    total = max(0, int(seconds))
    if total < 60:
        return "不到 1 分钟"
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = []
    if days:
        parts.append(f"{days} 天")
    if hours:
        parts.append(f"{hours} 小时")
    if minutes and not days:
        parts.append(f"{minutes} 分钟")
    return " ".join(parts)


def since_text(start: float | None, *, now: float, end: float | None = None) -> str:
    """The 开始于 value: when it started and how long it has lasted (or lasted)."""
    if start is None:
        return "未知"
    if end is not None:
        return f"{format_time(start)} → {format_time(end)}(共 {format_duration(end - start)})"
    return f"{format_time(start)}(已持续 {format_duration(now - start)})"


def redact_credentials(value: str) -> str:
    """``value`` without credentials in a DSN, bearer tokens or a custom-bot hook."""
    for pattern, replacement in _CREDENTIAL_REDACTIONS:
        value = pattern.sub(replacement, value)
    return value


def redact_secrets(value: str) -> str:
    """``redact_credentials`` plus the value given to a secret/token/password/api-key
    name (#905: what a log tail or a piece of evidence carries to Feishu)."""
    return _SECRET_VALUE.sub(r"\1\2***", redact_credentials(value))


@dataclass(frozen=True)
class PagerItem:
    """One failing (or recovered) object, in the field order of ``PAGER_FIELDS``."""

    level: str = ""
    environment: str = ""
    target: str = ""
    symptom: str = ""
    since: str = ""
    impact: str = ""
    action: str = ""
    runbook: str = ""
    log: str = ""
    #: shown next to the item number, e.g. 仍在告警 for a re-sent page
    note: str = ""

    def fields(self) -> list[tuple[str, str]]:
        values = (
            self.level,
            self.environment,
            self.target,
            self.symptom,
            self.since,
            self.impact,
            self.action,
            self.runbook,
            self.log,
        )
        return [(label, value) for label, value in zip(PAGER_FIELDS, values) if value]

    def summary_line(self) -> str:
        """The item in one line (级别 · 环境 · 对象 · 现象 · 开始于)."""
        parts = (
            self.level,
            self.environment,
            _one_line(self.target),
            _clip(_one_line(self.symptom), MAX_SUMMARY_CHARS),
            self.since,
        )
        line = " · ".join(_one_line(_clean(part)) for part in parts if part)
        return _clip(line, MAX_SUMMARY_LINE_CHARS)


@dataclass(frozen=True)
class PagerMessage:
    """A pager (or report) message before it is rendered as a card or as text."""

    title: str
    firing: tuple[PagerItem, ...] = ()
    resolved: tuple[PagerItem, ...] = ()
    preamble: tuple[str, ...] = ()
    #: a report: compact, one line per item, never the full blocks
    report: bool = False


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


#: a chat id as long as Feishu's (``oc_`` + 32 hex), for measuring an app-mode body
_CHAT_ID_PLACEHOLDER = "oc_" + "0" * 32


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


def _safe_url(value: object) -> str:
    """``value`` as a link target when it is a short http(s) URL with nothing that could
    close the markup around it; "" otherwise."""
    text = str(value or "").strip()
    if len(text) > MAX_URL_CHARS or not text.startswith(("https://", "http://")):
        return ""
    if any(char.isspace() or char in '()[]<>"`\\' for char in text):
        return ""
    return text


def _md_div(content: str) -> dict[str, Any]:
    """Markup we write ourselves (a heading); never a value."""
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _text_div(content: str) -> dict[str, Any]:
    return {"tag": "div", "text": {"tag": "plain_text", "content": content}}


def _clean(value: object) -> str:
    return _CONTROL_CHARS.sub("", str(value))


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _epoch(value: object) -> float | None:
    """Epoch seconds from an Alertmanager time or a number; None when it is no time.

    RFC 3339 strings and epoch numbers (a number above 1e12 is milliseconds) are
    read. Go's zero time, anything before 2001 or after 2100 (a non-finite number
    included) is unknown: a malformed time must never break a page.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        epoch = float(value)
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            epoch = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            try:
                epoch = parsed.timestamp()
            except (OverflowError, OSError, ValueError):
                return None
    if epoch > 1e12:
        epoch /= 1000.0
    return epoch if _EARLIEST_EPOCH <= epoch <= _LATEST_EPOCH else None


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
        item = _payload_item(
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
        message = pager_message_from_payload(payload, now=now)
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
    return render_pager_text(pager_message_from_payload(payload, now=now))


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
    channel_ids: list[str],
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
    from libs.service_identity import ServiceIdentity

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
                        "channels": [
                            channel_id for channel_id in channel_ids if channel_id
                        ],
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
    channel_ids: list[str],
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
    from libs.service_identity import ServiceIdentity

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
                        "channels": [
                            channel_id for channel_id in channel_ids if channel_id
                        ],
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


def deliver_feishu_text(
    webhook_url: str, text: str, timeout: float = 10.0
) -> dict[str, Any]:
    """Send a text message to Feishu (custom bot) and return the decoded response."""
    return _deliver_feishu_webhook(
        webhook_url, build_feishu_text_payload(text), timeout=timeout
    )


def deliver_feishu_card(
    webhook_url: str, card: dict[str, Any], timeout: float = 10.0
) -> dict[str, Any]:
    """Send an interactive card to Feishu (custom bot) and return the decoded response."""
    return _deliver_feishu_webhook(
        webhook_url, build_feishu_card_payload(card), timeout=timeout
    )


def _deliver_feishu_webhook(
    webhook_url: str, payload: dict[str, Any], *, timeout: float
) -> dict[str, Any]:
    safe_url = validate_feishu_webhook_url(webhook_url)
    request = Request(
        safe_url,
        data=feishu_request_body(payload),
        headers={"Content-Type": JSON_UTF8},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            response_body = response.read().decode("utf-8")
    except OSError as exc:
        raise FeishuDeliveryError("Feishu webhook delivery failed") from exc

    try:
        decoded = json.loads(response_body) if response_body else {}
    except json.JSONDecodeError as exc:
        raise FeishuDeliveryError("Feishu webhook returned invalid JSON") from exc

    code = decoded.get("code")
    if code not in (None, 0):
        message = decoded.get("msg") or decoded.get("message") or "unknown error"
        raise FeishuDeliveryError(f"Feishu webhook rejected message: {message}")
    return decoded


def deliver_feishu_app_text(
    *,
    app_id: str,
    app_secret: str,
    chat_id: str,
    text: str,
    api_base: str = "https://open.feishu.cn",
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Send a text message to a Feishu chat using app bot OpenAPI."""
    safe_chat_id = _required("FEISHU_CHAT_ID", chat_id)
    return _deliver_feishu_app_message(
        app_id=app_id,
        app_secret=app_secret,
        message_payload=build_feishu_app_message_payload(safe_chat_id, text),
        api_base=api_base,
        timeout=timeout,
    )


def deliver_feishu_app_card(
    *,
    app_id: str,
    app_secret: str,
    chat_id: str,
    card: dict[str, Any],
    api_base: str = "https://open.feishu.cn",
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Send an interactive card to a Feishu chat using app bot OpenAPI."""
    safe_chat_id = _required("FEISHU_CHAT_ID", chat_id)
    return _deliver_feishu_app_message(
        app_id=app_id,
        app_secret=app_secret,
        message_payload=build_feishu_app_card_payload(safe_chat_id, card),
        api_base=api_base,
        timeout=timeout,
    )


def _deliver_feishu_app_message(
    *,
    app_id: str,
    app_secret: str,
    message_payload: dict[str, Any],
    api_base: str,
    timeout: float,
) -> dict[str, Any]:
    base = validate_feishu_api_base(api_base)
    safe_app_id = _required("FEISHU_APP_ID", app_id)
    safe_app_secret = _required("FEISHU_APP_SECRET", app_secret)

    token_response = _post_json(
        f"{base}/open-apis/auth/v3/tenant_access_token/internal",
        {
            "app_id": safe_app_id,
            "app_secret": safe_app_secret,
        },
        timeout=timeout,
    )
    access_token = token_response.get("tenant_access_token")
    if not access_token:
        raise FeishuDeliveryError("Feishu tenant_access_token missing in response")

    return _post_json(
        f"{base}/open-apis/im/v1/messages?receive_id_type=chat_id",
        message_payload,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=timeout,
    )


#: The credentials deliver_infra2_report needs, in (app id, app secret, chat id) order.
INFRA2_REPORTS_ENV = (
    "INFRA2_REPORTS_FEISHU_APP_ID",
    "INFRA2_REPORTS_FEISHU_APP_SECRET",
    "INFRA2_REPORTS_FEISHU_CHAT_ID",
)


def deliver_infra2_report(text: str, env: "Mapping[str, str] | None" = None) -> bool:
    """Post a daily-report message to the shared 'infra2 reports' Lark group via the infra2
    Feishu app bot. The single delivery entry every periodic REPORT reconciler uses (DNS drift,
    config drift, …) so the channel + credentials live in one place, not per-tool.

    Reads ``INFRA2_REPORTS_ENV`` (+ optional ``INFRA2_REPORTS_FEISHU_API_BASE``).
    Returns True if delivered, False if not configured (so a not-yet-wired reconciler no-ops
    cleanly instead of erroring). A configured-but-failing delivery raises (a broken report
    path must be visible, not silently green).

    Every report starts with ``REPORT_TITLE_PREFIX`` (#905), added here once for all
    senders, so a report never reads like a page.
    """
    e = os.environ if env is None else env
    app_id, app_secret, chat_id = (e.get(name, "") for name in INFRA2_REPORTS_ENV)
    if not (app_id and app_secret and chat_id):
        return False
    deliver_feishu_app_text(
        app_id=app_id,
        app_secret=app_secret,
        chat_id=chat_id,
        text=as_report(text),
        api_base=e.get("INFRA2_REPORTS_FEISHU_API_BASE", "https://open.feishu.cn"),
    )
    return True


def as_report(text: str) -> str:
    """``text`` with the ``[报告]`` header (#905), added once."""
    stripped = text.lstrip()
    return (
        stripped
        if stripped.startswith(REPORT_TITLE_PREFIX)
        else (REPORT_TITLE_PREFIX + stripped)
    )


def validate_feishu_api_base(api_base: str) -> str:
    """Validate Feishu/Lark OpenAPI base URL."""
    candidate = (api_base or "https://open.feishu.cn").strip().rstrip("/")
    parsed = urlparse(candidate)
    if parsed.scheme != "https":
        raise InvalidFeishuAppConfig("Feishu API base must use https")
    if parsed.hostname not in FEISHU_WEBHOOK_HOSTS:
        allowed = ", ".join(sorted(FEISHU_WEBHOOK_HOSTS))
        raise InvalidFeishuAppConfig(f"Feishu API host must be one of: {allowed}")
    return candidate


def feishu_host_reachable(url: str, timeout: float = 3.0) -> bool:
    """Best-effort TCP reachability check to the Feishu/Lark host (port 443).

    Proves the bridge can *reach* Feishu without POSTing anything — so a
    "lark 畅通" probe can run every minute without spamming the real alert
    channel. Returns True iff a TCP connection to (host, 443) opens. Never
    raises; an unparseable/empty URL or any socket error returns False.
    """
    import socket

    host = urlparse((url or "").strip()).hostname
    if not host:
        return False
    try:
        with socket.create_connection((host, 443), timeout=timeout):
            return True
    except OSError:
        return False


def redacted_url(url: str) -> str:
    """Return a webhook URL without the secret token."""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return "***"
    return f"{parsed.scheme}://{parsed.netloc}/open-apis/bot/v2/hook/***"


def redacted_app_config(app_id: str, chat_id: str, api_base: str) -> dict[str, str]:
    """Return safe Feishu app delivery metadata."""
    redacted_app_id = f"{app_id[:8]}..." if app_id else ""
    redacted_chat_id = f"{chat_id[:8]}..." if chat_id else ""
    return {
        "api_base": validate_feishu_api_base(api_base),
        "app_id": redacted_app_id,
        "chat_id": redacted_chat_id,
    }


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: float,
) -> dict[str, Any]:
    request_headers = {"Content-Type": JSON_UTF8}
    request_headers.update(headers or {})
    request = Request(
        url,
        data=feishu_request_body(payload),
        headers=request_headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            response_body = response.read().decode("utf-8")
    except OSError as exc:
        raise FeishuDeliveryError("Feishu OpenAPI request failed") from exc

    try:
        decoded = json.loads(response_body) if response_body else {}
    except json.JSONDecodeError as exc:
        raise FeishuDeliveryError("Feishu OpenAPI returned invalid JSON") from exc

    code = decoded.get("code")
    if code not in (None, 0):
        message = decoded.get("msg") or decoded.get("message") or "unknown error"
        raise FeishuDeliveryError(f"Feishu OpenAPI rejected message: {message}")
    return decoded


def deliver_out_of_band_text(
    env: Mapping[str, str], text: str, *, timeout: float = 10.0
) -> dict[str, Any]:
    """Deliver text through the out-of-band Feishu webhook/app path.

    Shared by the weekly digest, the positive stability report, and the Google
    Drive sync token-expiry alert so every out-of-band sender selects the
    delivery mode identically. Prefers ``INFRA2_OUT_OF_BAND_*`` settings (used in
    GitHub Actions) and falls back to the generic ``ALERT_DELIVERY_MODE`` /
    ``FEISHU_*`` names.
    """
    mode = (
        env.get("INFRA2_OUT_OF_BAND_ALERT_DELIVERY_MODE")
        or env.get("ALERT_DELIVERY_MODE")
        or "feishu_webhook"
    ).strip()
    if mode == "feishu_app":
        return deliver_feishu_app_text(
            app_id=env.get("INFRA2_OUT_OF_BAND_FEISHU_APP_ID")
            or env.get("FEISHU_APP_ID", ""),
            app_secret=env.get("INFRA2_OUT_OF_BAND_FEISHU_APP_SECRET")
            or env.get("FEISHU_APP_SECRET", ""),
            chat_id=env.get("INFRA2_OUT_OF_BAND_FEISHU_CHAT_ID")
            or env.get("FEISHU_CHAT_ID", ""),
            api_base=env.get("INFRA2_OUT_OF_BAND_FEISHU_API_BASE")
            or env.get("FEISHU_API_BASE", "https://open.feishu.cn"),
            text=text,
            timeout=timeout,
        )
    webhook_url = (
        env.get("INFRA2_OUT_OF_BAND_FEISHU_WEBHOOK_URL")
        or env.get("FEISHU_WEBHOOK_URL")
        or ""
    ).strip()
    if not webhook_url:
        raise InvalidFeishuAppConfig(
            "Feishu webhook URL or app credentials are required for out-of-band delivery"
        )
    return deliver_feishu_text(webhook_url, text, timeout=timeout)


def _required(name: str, value: str) -> str:
    candidate = (value or "").strip()
    if not candidate:
        raise InvalidFeishuAppConfig(f"{name} is required")
    return candidate


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _one_line(value: Any) -> str:
    return " ".join(str(value).split())


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


def _truncate_message(message: str) -> str:
    return (
        message[: MAX_MESSAGE_CHARS - len(TRUNCATION_SUFFIX)].rstrip()
        + TRUNCATION_SUFFIX
    )
