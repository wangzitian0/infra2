"""Domain entities, constants, and formatting utilities for alerting."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

FEISHU_WEBHOOK_HOSTS = {"open.feishu.cn", "open.larksuite.com"}
FEISHU_WEBHOOK_PATH_PREFIX = "/open-apis/bot/v2/hook/"
SIGNOZ_ALERT_SCHEMA_VERSION = "v2alpha1"
SIGNOZ_ALERT_VERSION = "v5"
SIGNOZ_FEISHU_CHANNEL_PREFIX = "infra2-feishu-alerts"
ERROR_LOG_LEVELS = ("ERROR", "CRITICAL", "FATAL")
MAX_MESSAGE_CHARS = 3500
JSON_UTF8 = "application/json; charset=utf-8"
TRUNCATION_SUFFIX = "\n...[truncated]"
DELIVERY_LABEL = "delivery"
REPORT_DELIVERY = "report"
REPORT_TITLE_PREFIX = "[报告] "

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
DISPLAY_TIMEZONE = timezone(timedelta(hours=8))
DISPLAY_TIMEZONE_LABEL = "UTC+8"
MAX_FULL_ITEMS = 5
MAX_SUMMARY_ITEMS = 20
MAX_FIELD_CHARS = 300
MAX_SUMMARY_CHARS = 160
MAX_SUMMARY_LINE_CHARS = 240
MAX_TITLE_NAME_CHARS = 80
MAX_URL_CHARS = 500
MAX_LOG_CHARS = 800
MAX_LOG_LINES = 8
FEISHU_BODY_LIMITS = {"feishu_webhook": 20_000, "feishu_app": 30_000}
_BODY_MARGIN = 1_024
_EARLIEST_EPOCH = 1_000_000_000  # 2001-09-09
_LATEST_EPOCH = 4_102_444_800  # 2100-01-01

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
RUNBOOK_SECTION = (
    f"{_REPO_BLOB}/docs/ssot/ops.observability.md#7-标准操作程序-playbooks"
)

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

REPORT_ONLY_ENVIRONMENTS = frozenset({"staging", "preview"})


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


def pager_level(severity: object) -> str:
    """Map a raw severity string or object to P0, P1, or P2."""
    if not isinstance(severity, str):
        return "P0"
    return _LEVEL_BY_SEVERITY.get(severity.strip().lower(), "P0")


def highest_level(levels: Iterable[str]) -> str:
    """Return the most severe level across levels, defaulting to P0."""
    selected = "P2"
    found = False
    for raw in levels:
        level = pager_level(raw)
        found = True
        if _LEVEL_RANK[level] < _LEVEL_RANK[selected]:
            selected = level
            if selected == "P0":
                return "P0"
    return selected if found else "P0"


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
    """Redact bearer tokens and DSN credentials."""
    result = value
    for pattern, replacement in _CREDENTIAL_REDACTIONS:
        result = pattern.sub(replacement, result)
    return result


def redact_secrets(value: str) -> str:
    """Redact passwords and tokens."""
    cleaned = redact_credentials(value)
    return _SECRET_VALUE.sub(r"\1\2***", cleaned)


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


def _one_line(value: Any) -> str:
    return " ".join(str(value).split())


def _truncate_message(message: str) -> str:
    return (
        message[: MAX_MESSAGE_CHARS - len(TRUNCATION_SUFFIX)].rstrip()
        + TRUNCATION_SUFFIX
    )


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _required(name: str, value: str) -> str:
    candidate = (value or "").strip()
    if not candidate:
        raise InvalidFeishuAppConfig(f"{name} is required")
    return candidate


@dataclass(frozen=True)
class PagerItem:
    """One failure in a pager message."""

    level: str = ""
    environment: str = ""
    target: str = ""
    symptom: str = ""
    since: str = ""
    impact: str = ""
    action: str = ""
    runbook: str = ""
    log: str = ""
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
    report: bool = False
