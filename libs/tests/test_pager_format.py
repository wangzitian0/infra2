"""#905: every pager message reads the same way (ops.observability.md §3.1).

Each in-band source builds its real payload here -- probe, public route, container
breakdown, deploy queue, a SigNoz rule -- and the bridge renders it. Every field of
``PAGER_FIELDS`` is asserted by value, on the card the bridge sends and on its text
twin. The Worker and the GitHub watchdog are held to the same layout in
test_cloudflare_watchdog.py and test_out_of_band_watchdog.py.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import replace
from pathlib import Path

import pytest

from libs import alerting
from libs.alerting import (
    DEFAULT_ACTION,
    DEFAULT_IMPACT,
    FIELD_SEPARATOR,
    FEISHU_BODY_LIMITS,
    MAX_FIELD_CHARS,
    MAX_MESSAGE_CHARS,
    PAGER_FIELDS,
    REPORT_TITLE_PREFIX,
    RUNBOOK_SECTION,
    build_feishu_alert_card,
    build_signoz_metric_alert_rule_payload,
    card_body_bytes,
    card_budget,
    format_signoz_alert,
    mark_report_payload,
    pager_level,
)
from libs.deploy_queue import (
    ComposeDeployments,
    build_deploy_guard_alert_payload,
    find_stuck_deploys,
)
from libs.observability import probes
from libs.observability.breakdown import (
    build_breakdown_alert_payload,
    find_breakdown_containers,
)

ROOT = Path(__file__).resolve().parents[2]
#: 2026-09-24 08:00 UTC
T0 = 1_790_236_800
BLOB = "https://github.com/wangzitian0/infra2/blob/main"


def _text_items(text: str) -> list[dict[str, str]]:
    """The item blocks of a rendered text: ``{label: value}`` in message order."""
    items: list[dict[str, str]] = []
    logging = False
    for line in text.splitlines():
        label, separator, value = line.partition(FIELD_SEPARATOR)
        if re.fullmatch(r"— \d+/\d+( · .+)? —", line):
            items.append({})
            logging = False
        elif items and separator and label in PAGER_FIELDS:
            items[-1][label] = value
            logging = label == "日志"
        elif logging and line:  # the log tail's lines follow its label
            items[-1]["日志"] += ("\n" if items[-1]["日志"] else "") + line
        else:
            logging = False
    return items


def _card_fields(card: dict) -> list[list[tuple[str, str, str]]]:
    """The field blocks of a card: ``(text tag, label, value)`` per field."""
    blocks = []
    for element in card["elements"]:
        if "fields" not in element:
            continue
        block = []
        for field in element["fields"]:
            text = field["text"]
            pattern = (
                (r"\*\*(Runbook)\*\*" if text["tag"] == "lark_md" else r"(\S+?)")
                + FIELD_SEPARATOR
                + r"(.*)"
            )
            match = re.fullmatch(pattern, text["content"], re.S)
            assert match, text
            block.append((text["tag"], match.group(1), match.group(2).lstrip("\n")))
        blocks.append(block)
    return blocks


def _card_items(card: dict) -> list[dict[str, str]]:
    """The field blocks of a card: ``{label: value}`` in card order."""
    return [
        {label: value for _tag, label, value in block} for block in _card_fields(card)
    ]


def _probe_payload(monkeypatch, *, name, target, expected, reading, alert_name):
    monkeypatch.setenv("INFRA_ENVIRONMENT", "production")
    spec = probes.ProbeSpec(
        name=name,
        kind="http",
        target=target,
        expected=expected,
        severity="critical",
        service_id="finance_report/app",
    )
    result = probes.run_probe(spec, http_get=lambda *_args: reading)
    return probes.build_probe_alert_payload(
        [result], alert_name=alert_name, started_at={name: T0 - 720}
    )


def test_a_probe_page_shows_every_field(monkeypatch) -> None:
    payload = _probe_payload(
        monkeypatch,
        name="finance-report-backend-http",
        target="http://finance-report-backend:8000/api/health",
        expected="200",
        reading=(502, "Bad Gateway"),
        alert_name="InfraServiceProbeFailed",
    )

    (item,) = _text_items(format_signoz_alert(payload, now=T0))

    assert item == {
        "级别": "P0",
        "环境": "production",
        "对象": "finance_report/app · finance-report-backend-http",
        "现象": "http http://finance-report-backend:8000/api/health → 期望 200; "
        "实际 502:Bad Gateway",
        "开始于": "2026-09-24 15:48（UTC+8）(已持续 12 分钟)",
        "影响": "[service-or-route] " + alerting._IMPACT_BY_DOMAIN["service-or-route"],
        "下一步": alerting._ACTION_BY_DOMAIN["service-or-route"],
        "Runbook": f"{BLOB}/platform/12.alerting/README.md#infra-service-probes",
    }


def test_a_public_route_blocked_at_the_edge_says_so(monkeypatch) -> None:
    payload = _probe_payload(
        monkeypatch,
        name="finance-report-web-public-route",
        target="https://report.example.invalid/",
        expected="200,302,307,308",
        reading=(403, "error code: 1010"),
        alert_name="InfraPublicRouteProbeFailed",
    )

    (item,) = _text_items(format_signoz_alert(payload, now=T0))

    assert item["对象"] == "finance_report/app · finance-report-web-public-route"
    assert item["现象"] == (
        "http https://report.example.invalid/ → 期望 200,302,307,308; "
        "实际 403:error code: 1010"
    )
    assert (
        item["影响"]
        == "[probe-client-blocked] "
        + (alerting._IMPACT_BY_DOMAIN["probe-client-blocked"])
    )
    assert item["下一步"] == alerting._ACTION_BY_DOMAIN["probe-client-blocked"]
    assert (
        item["Runbook"] == f"{BLOB}/platform/12.alerting/README.md#public-route-probes"
    )
    assert set(item) == set(PAGER_FIELDS) - {"日志"}


def _breakdown_payload(logs: str) -> dict:
    entry = {
        "Id": "c8f0c703f3d4",
        "Names": ["/finance_report-backend"],
        "State": "restarting",
        "Status": "Restarting (1) 3 seconds ago",
        "Labels": {
            "party.zitian.infra.service-id": "finance_report/app",
            "party.zitian.infra.component": "backend",
            "party.zitian.infra.environment": "production",
        },
    }
    (breakdown,) = find_breakdown_containers([entry], lambda _id: logs)
    return build_breakdown_alert_payload([replace(breakdown, since=T0 - 180)])


def test_a_container_breakdown_shows_its_log_tail() -> None:
    logs = (
        "2026-09-24T07:56:58Z booting\n"
        "VAULT_ROLE_ID and VAULT_SECRET_ID are required\n"
        "2026-09-24T07:56:59Z exit status 1\n"
    )
    (item,) = _text_items(format_signoz_alert(_breakdown_payload(logs), now=T0))

    assert item == {
        "级别": "P0",
        "环境": "production",
        "对象": "finance_report/app · finance_report-backend",
        "现象": "restarting: Vault AppRole 凭据缺失(VAULT_ROLE_ID / VAULT_SECRET_ID)",
        "开始于": "2026-09-24 15:57（UTC+8）(已持续 3 分钟)",
        "影响": "[runtime] " + alerting._IMPACT_BY_DOMAIN["runtime"],
        "下一步": alerting._ACTION_BY_DOMAIN["runtime"],
        "Runbook": f"{BLOB}/docs/runbooks/infra022-p0.md#container-killed",
        "日志": "2026-09-24T07:56:58Z booting\n"
        "VAULT_ROLE_ID and VAULT_SECRET_ID are required\n"
        "2026-09-24T07:56:59Z exit status 1",
    }


def test_the_log_tail_keeps_the_evidence_and_the_newest_lines() -> None:
    """The line the reason came from, then the last lines, each trimmed."""
    noise = "".join(f"line {index}\n" for index in range(40))
    logs = "permission denied opening /vault/secrets\n" + noise + "x" * 500 + "\n"

    (item,) = _text_items(format_signoz_alert(_breakdown_payload(logs), now=T0))

    assert item["日志"].splitlines() == [
        "permission denied opening /vault/secrets",
        "…",
        "line 36",
        "line 37",
        "line 38",
        "line 39",
        "x" * 200,
    ]


def test_a_stuck_deploy_shows_the_queue_impact() -> None:
    compose = ComposeDeployments(
        compose_id="c1",
        compose_name="finance_report-app",
        service_id="finance_report/app",
        environment="production",
        deployments=(
            {
                "status": "running",
                "deploymentId": "d1",
                "startedAt": "2026-09-24T07:20:00.000Z",
            },
        ),
    )
    payload = build_deploy_guard_alert_payload(
        find_stuck_deploys([compose], T0, ceiling_seconds=1800)
    )

    (item,) = _text_items(format_signoz_alert(payload, now=T0))

    assert item == {
        "级别": "P0",
        "环境": "production",
        "对象": "finance_report/app · finance_report-app",
        "现象": "部署 d1 已运行 2400s,超过上限",
        "开始于": "2026-09-24 15:20（UTC+8）(已持续 40 分钟)",
        "影响": "[deploy-queue] 部署队列单并发 FIFO:它阻塞之后的所有部署",
        "下一步": alerting._ACTION_BY_DOMAIN["deploy-queue"],
        "Runbook": f"{BLOB}/docs/runbooks/infra022-p0.md#deployment-failed",
    }


def _signoz_payload(status: str = "firing") -> dict:
    """What SigNoz's Alertmanager POSTs for a rule this repo renders."""
    rule = build_signoz_metric_alert_rule_payload(
        alert_name="FinanceReportHigh5xxRate",
        promql="sum(rate(http_server_request_count[5m]))",
        channel_ids=["channel-1"],
        summary="finance_report backend 5xx rate is above 5% for 5 minutes.",
        service_name="finance-report-backend",
        severity="critical",
        service_id="finance_report/app",
        environment="production",
    )
    labels = {"alertname": rule["alert"], **rule["labels"], "ruleId": "0199a1b2"}
    return {
        "receiver": "infra2-feishu-alerts-production",
        "status": status,
        "alerts": [
            {
                "status": status,
                "labels": labels,
                "annotations": rule["annotations"],
                "startsAt": "2026-09-24T07:54:30.123456789Z",
                "endsAt": "0001-01-01T00:00:00Z"
                if status == "firing"
                else "2026-09-24T08:00:00.5Z",
                "generatorURL": "https://signoz.example.invalid/alerts/edit?ruleId=1",
                "fingerprint": "5c1f",
            }
        ],
        "groupLabels": {"alertname": rule["alert"]},
        "commonLabels": labels,
        "commonAnnotations": rule["annotations"],
        "externalURL": "https://signoz.example.invalid",
        "version": "4",
    }


def test_a_signoz_rule_page_shows_every_field() -> None:
    card = build_feishu_alert_card(_signoz_payload(), now=T0)
    (item,) = _card_items(card)

    assert item == {
        "级别": "P0",
        "环境": "production",
        "对象": "finance_report/app · finance-report-backend",
        "现象": "finance_report backend 5xx rate is above 5% for 5 minutes.",
        "开始于": "2026-09-24 15:54（UTC+8）(已持续 5 分钟)",
        "影响": DEFAULT_IMPACT,
        "下一步": DEFAULT_ACTION,
        "Runbook": f"[ops.observability.md#7-标准操作程序-playbooks]({RUNBOOK_SECTION})",
    }
    assert card["header"]["title"]["content"] == (
        "🔴 [P0 告警] FinanceReportHigh5xxRate · production · 1 项"
    )
    (action,) = [e for e in card["elements"] if e["tag"] == "action"]
    assert action["actions"][0]["url"] == "https://signoz.example.invalid"


def test_card_fields_are_separate_card_fields_in_the_shared_order(monkeypatch) -> None:
    """#905: the card is laid out as card fields, one per label, never jammed text."""
    payloads = [
        _probe_payload(
            monkeypatch,
            name="vault-http",
            target="http://vault:8200/v1/sys/health",
            expected="200",
            reading=(503, "sealed"),
            alert_name="InfraServiceProbeFailed",
        ),
        _breakdown_payload("connection refused\n"),
        _signoz_payload(),
    ]
    for payload in payloads:
        card = build_feishu_alert_card(payload, now=T0)
        (item,) = _card_items(card)
        order = [label for label in PAGER_FIELDS if label in item]
        assert list(item) == order
        assert {
            "级别",
            "环境",
            "对象",
            "现象",
            "开始于",
            "影响",
            "下一步",
            "Runbook",
        } <= set(item)
        (fields,) = [e["fields"] for e in card["elements"] if "fields" in e]
        assert len(fields) == len(item)
        # one field per row: short fields sit side by side and read as one line
        assert {field["is_short"] for field in fields} == {False}
        # values are plain text; only the runbook link is markup
        (block,) = _card_fields(card)
        assert {label for tag, label, _value in block if tag == "lark_md"} == {
            "Runbook"
        }


def test_resolved_names_what_recovered_and_for_how_long() -> None:
    card = build_feishu_alert_card(_signoz_payload("resolved"), now=T0 + 600)
    (item,) = _card_items(card)

    assert card["header"]["template"] == "green"
    assert card["header"]["title"]["content"] == (
        "✅ [已恢复] FinanceReportHigh5xxRate · production · 1 项"
    )
    assert item == {
        "级别": "P0",
        "环境": "production",
        "对象": "finance_report/app · finance-report-backend",
        "现象": "finance_report backend 5xx rate is above 5% for 5 minutes.",
        "开始于": "2026-09-24 15:54（UTC+8） → 2026-09-24 16:00（UTC+8）(共 5 分钟)",
    }


def test_a_resolved_breakdown_carries_its_start_and_end() -> None:
    payload = build_breakdown_alert_payload(
        [
            replace(
                find_breakdown_containers(
                    [{"Id": "1", "Names": ["/prefect-worker"], "State": "restarting"}],
                    lambda _id: "OOM\n",
                )[0],
                since=T0 - 5400,
            )
        ],
        firing=False,
        now=T0,
    )

    (item,) = _text_items(format_signoz_alert(payload, now=T0 + 999))

    assert item["对象"] == "infra/unregistered · prefect-worker"
    assert (
        item["开始于"]
        == "2026-09-24 14:30（UTC+8） → 2026-09-24 16:00（UTC+8）(共 1 小时 30 分钟)"
    )


@pytest.mark.parametrize(
    ("severity", "level", "template"),
    [
        ("critical", "P0", "red"),
        ("error", "P1", "orange"),
        ("warning", "P2", "yellow"),
        ("P1", "P1", "orange"),
        ("CRITICAL", "P0", "red"),
        ("", "P0", "red"),
        ("sev-typo", "P0", "red"),
    ],
)
def test_severity_maps_to_one_vocabulary(severity, level, template) -> None:
    """§3: critical = P0, error = P1, warning = P2; unknown counts as critical."""
    payload = {
        "status": "firing",
        "commonLabels": {"alertname": "X", "severity": severity},
        "alerts": [{"status": "firing", "labels": {"alertname": "X"}}],
    }
    card = build_feishu_alert_card(payload, now=T0)
    (item,) = _card_items(card)

    assert pager_level(severity) == level
    assert item["级别"] == level
    assert card["header"]["template"] == template
    assert card["header"]["title"]["content"].startswith(
        f"{alerting._LEVEL_EMOJI[level]} [{level} 告警] X"
    )


def test_the_title_carries_the_most_severe_level_and_it_comes_first() -> None:
    payload = {
        "status": "firing",
        "commonLabels": {
            "alertname": "InfraServiceProbeFailed",
            "severity": "critical",
        },
        "alerts": [
            {"labels": {"component": "openpanel-worker-http", "severity": "warning"}},
            {"labels": {"component": "openpanel-roundtrip", "severity": "error"}},
        ],
    }
    card = build_feishu_alert_card(payload, now=T0)

    assert card["header"]["title"]["content"].startswith("🟠 [P1 告警] ")
    assert [item["对象"] for item in _card_items(card)] == [
        "openpanel-roundtrip",
        "openpanel-worker-http",
    ]


def test_a_report_is_titled_as_one_and_compact() -> None:
    """#905: a `delivery=report` payload is not a page: `[报告]`, blue, one line each."""
    payload = mark_report_payload(_signoz_payload())
    card = build_feishu_alert_card(payload, now=T0)
    text = format_signoz_alert(payload, now=T0)

    assert card["header"]["template"] == "blue"
    assert card["header"]["title"]["content"] == (
        f"{REPORT_TITLE_PREFIX}FinanceReportHigh5xxRate · production · 1 项"
    )
    assert _card_items(card) == []
    (body,) = [e["text"]["content"] for e in card["elements"] if e["tag"] == "div"]
    assert body == (
        "• P0 · production · finance_report/app · finance-report-backend · "
        "finance_report backend 5xx rate is above 5% for 5 minutes. · "
        "2026-09-24 15:54（UTC+8）(已持续 5 分钟)"
    )
    assert text.splitlines() == [card["header"]["title"]["content"], body]


def test_deliver_infra2_report_titles_every_report_once(monkeypatch) -> None:
    sent = []
    monkeypatch.setattr(
        alerting,
        "deliver_feishu_app_text",
        lambda **kwargs: sent.append(kwargs["text"]),
    )
    env = {"INFRA2_REPORTS_FEISHU_APP_ID": "a", "INFRA2_REPORTS_FEISHU_APP_SECRET": "b"}
    env["INFRA2_REPORTS_FEISHU_CHAT_ID"] = "c"

    assert alerting.deliver_infra2_report("📋 [Infra2] config-drift", env)
    assert alerting.deliver_infra2_report("[报告] already titled", env)
    assert sent == ["[报告] 📋 [Infra2] config-drift", "[报告] already titled"]


def _huge_payload(count: int) -> dict:
    chinese = "服务不可用" * 1000  # 5,000 characters, 6 bytes each once ASCII-escaped
    return {
        "status": "firing",
        "commonLabels": {"alertname": "ContainerBreakdown", "severity": "critical"},
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "service_id": f"infra/svc{index}",
                    "environment": "production",
                    "severity": "critical",
                    "failure_domain": "runtime",
                },
                "annotations": {
                    "container": f"svc{index}-container",
                    "symptom": chinese,
                    "log_tail": "\n".join([chinese[:400]] * 30),
                },
                "startsAt": "2026-09-24T07:00:00Z",
            }
            for index in range(count)
        ],
    }


@pytest.mark.parametrize("mode", sorted(FEISHU_BODY_LIMITS))
def test_many_long_alerts_stay_inside_the_limit_of_the_mode_that_sends_them(
    mode,
) -> None:
    """#905: 60 alerts of 5,000-character symptoms and 12,000-character logs. The card
    fits the body limit of its delivery mode (webhook 20 KB, app 30 KB), the text
    3,500 characters; the first alerts are shown in full, every other one is still
    counted."""
    payload = _huge_payload(60)
    card = build_feishu_alert_card(payload, now=T0, delivery_mode=mode)
    text = format_signoz_alert(payload, now=T0)
    full = _card_items(card)

    assert card_body_bytes(card, mode) <= card_budget(mode) < FEISHU_BODY_LIMITS[mode]
    assert card["header"]["title"]["content"].endswith(" · 60 项")
    assert 1 <= len(full) < 60
    assert all(len(item["现象"]) == MAX_FIELD_CHARS for item in full)
    headings = [
        e["text"]["content"]
        for e in card["elements"]
        if "另有" in e.get("text", {}).get("content", "")
    ]
    assert headings == [f"**另有 {60 - len(full)} 项,只列摘要:**"]
    assert len(text) <= MAX_MESSAGE_CHARS
    assert text.splitlines()[0].endswith(" · 60 项")
    assert "另有 " in text


def test_a_few_short_alerts_are_all_shown_in_full() -> None:
    payload = _huge_payload(3)
    for index, alert in enumerate(payload["alerts"]):
        alert["annotations"] = {"container": f"svc{index}-container", "symptom": "down"}
    card = build_feishu_alert_card(payload, now=T0)

    assert [item["对象"] for item in _card_items(card)] == [
        f"infra/svc{index} · svc{index}-container" for index in range(3)
    ]
    assert not [
        e for e in card["elements"] if "另有" in json.dumps(e, ensure_ascii=False)
    ]


@pytest.mark.parametrize(
    "value",
    [
        'body=<at id="all"></at> <b>x</b>',
        "see [the fix](https://evil.example/steal) and **bold**",
    ],
)
def test_a_value_is_shown_never_interpreted(value) -> None:
    """#905: a value that holds an ``<at>`` tag or a markdown link is plain text on the
    card: it cannot mention everyone, link out or restyle the card."""
    payload = {
        "status": "firing",
        "commonLabels": {"alertname": "X", "severity": "critical"},
        "alerts": [{"annotations": {"symptom": value}}],
    }
    card = build_feishu_alert_card(payload, now=T0)
    (block,) = _card_fields(card)
    markup = [
        e["text"]["content"]
        for e in card["elements"]
        if e.get("text", {}).get("tag") == "lark_md"
    ] + [value for tag, _label, value in block if tag == "lark_md"]

    assert ("plain_text", "现象", value) in block
    assert not [content for content in markup if "evil" in content or "<at" in content]


# ---- the runbook links resolve ------------------------------------------------------


def _github_anchor(heading: str) -> str:
    """GitHub's heading anchor: lowercased, punctuation and symbols dropped (``-``
    and ``_`` kept), spaces to ``-``."""
    kept = (
        char
        for char in heading.strip().lower()
        if char in "-_ " or unicodedata.category(char)[0] not in "PS"
    )
    return "".join(kept).replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    return {
        _github_anchor(line.lstrip("#"))
        for line in text.splitlines()
        if re.match(r"#{1,6} ", line)
    }


def test_the_anchor_slug_matches_githubs_on_a_known_link() -> None:
    """The slug rule reproduces an anchor already linked from this repo."""
    from tools.out_of_band_watchdog import STATE_DISCREPANCY_RUNBOOK

    anchor = STATE_DISCREPANCY_RUNBOOK.split("#", 1)[1]
    assert anchor in _anchors(ROOT / "docs/ssot/ops.standards.md")


def test_every_runbook_link_the_bridge_uses_resolves() -> None:
    urls = {
        RUNBOOK_SECTION,
        *alerting._RUNBOOK_BY_ALERT_OVERRIDE.values(),
        *alerting._RUNBOOK_BY_DOMAIN.values(),
        *alerting._RUNBOOK_BY_ALERT.values(),
    }
    missing = []
    for url in sorted(urls):
        path, anchor = url.removeprefix(f"{BLOB}/").split("#", 1)
        if anchor not in _anchors(ROOT / path):
            missing.append(url)
    assert len(urls) >= 9
    assert missing == []


def _resource_page(monkeypatch, name: str, target: str) -> dict[str, str]:
    """A host resource probe the production runner declares (platform/12.alerting
    /deploy.py), failing above its ceiling, as the card shows it."""
    monkeypatch.setenv("INFRA_ENVIRONMENT", "production")
    spec = probes.ProbeSpec(
        name=name,
        kind="resource",
        target=target,
        expected="80",
        service_id="infra/host",
    )
    result = probes.ProbeResult(
        spec=spec,
        ok=False,
        summary="expected '80', observed '91.0'",
        observed="91.0",
        elapsed_ms=1,
    )
    payload = probes.build_probe_alert_payload([result])
    assert payload["alerts"][0]["labels"]["failure_domain"] == f"host-{name[5:]}"
    (item,) = _text_items(format_signoz_alert(payload, now=T0))
    return item


@pytest.mark.parametrize(
    ("name", "target", "runbook"),
    [
        ("host-disk", "disk:/hostfs", "docs/runbooks/infra022-p0.md#disk-full"),
        ("host-mem", "mem", "platform/12.alerting/README.md#host-resource-probes"),
        ("host-cpu", "cpu", "platform/12.alerting/README.md#host-resource-probes"),
    ],
)
def test_a_host_resource_probe_reads_as_the_host(monkeypatch, name, target, runbook):
    """#905 review: host-cpu/mem/disk were `service-or-route`, so their card told the
    reader to curl a route. They have their own domains, impacts and next steps."""
    domain = f"host-{name[5:]}"
    item = _resource_page(monkeypatch, name, target)

    assert item["对象"] == f"infra/host · {name}"
    assert item["现象"] == f"resource {target} → 期望 80; 实际 91.0"
    assert item["影响"] == f"[{domain}] {alerting._IMPACT_BY_DOMAIN[domain]}"
    assert item["下一步"] == alerting._ACTION_BY_DOMAIN[domain]
    assert item["Runbook"] == f"{BLOB}/{runbook}"


def test_a_backup_verification_page_says_what_is_stale_and_why() -> None:
    """#905 review: `failure_domain=backup` had no impact, next step or runbook, and
    the card showed the evidence without the summary it belongs to."""
    from libs.backup.verification import build_backup_alert_payload

    report = {
        "checks": [
            {
                "service_id": "platform/postgres",
                "status": "fail",
                "severity": "P1",
                "summary": "backup artifact is stale",
                "evidence": {"age_hours": 200, "rpo_hours": 180},
            }
        ]
    }

    (item,) = _text_items(
        format_signoz_alert(build_backup_alert_payload(report), now=T0)
    )

    assert item["对象"] == "platform/postgres · backup"
    assert item["现象"] == (
        "backup artifact is stale — {'age_hours': 200, 'rpo_hours': 180}"
    )
    assert item["影响"] == "[backup] " + alerting._IMPACT_BY_DOMAIN["backup"]
    assert item["下一步"] == alerting._ACTION_BY_DOMAIN["backup"]
    assert item["Runbook"] == (
        f"{BLOB}/docs/ssot/ops.recovery.md#sop-004-备份-freshness-验证"
    )


def test_a_summary_equal_to_its_description_is_shown_once() -> None:
    (item,) = _card_items(build_feishu_alert_card(_signoz_payload(), now=T0))

    assert item["现象"] == "finance_report backend 5xx rate is above 5% for 5 minutes."


# ---- what Feishu is actually sent ------------------------------------------------------


class _FeishuServer:
    """Records every request the send path makes; answers as Feishu does."""

    def __init__(self) -> None:
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        body = b'{"code": 0, "tenant_access_token": "t-test"}'

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return body

        return _Response()


def _twenty_long_alerts() -> dict:
    """The reviewer's case: 20 alerts of long Chinese text."""
    payload = _huge_payload(20)
    for alert in payload["alerts"]:
        alert["annotations"]["symptom"] = "磁盘写满导致服务不可用" * 30
    return payload


def test_the_webhook_posts_utf8_under_its_20kb_limit(monkeypatch) -> None:
    """#905 review: the webhook limit is 20 KB, and ASCII-escaped JSON made a Chinese
    character cost 6 bytes (25,385 bytes for 20 alerts). The body is captured from the
    send path itself."""
    server = _FeishuServer()
    monkeypatch.setattr(alerting, "urlopen", server)
    card = build_feishu_alert_card(
        _twenty_long_alerts(), now=T0, delivery_mode="feishu_webhook"
    )

    alerting.deliver_feishu_card(
        "https://open.feishu.cn/open-apis/bot/v2/hook/test-token", card
    )

    (request,) = server.requests
    assert request.get_header("Content-type") == "application/json; charset=utf-8"
    assert len(request.data) <= FEISHU_BODY_LIMITS["feishu_webhook"] == 20_000
    assert json.loads(request.data.decode("utf-8"))["card"] == card
    assert "磁盘写满".encode() in request.data  # sent as UTF-8, not \\u escapes
    # the same card ASCII-escaped, as it used to be sent, would not fit
    ascii_body = json.dumps({"msg_type": "interactive", "card": card}).encode()
    assert len(ascii_body) > FEISHU_BODY_LIMITS["feishu_webhook"]


def test_the_app_bot_posts_utf8_under_its_30kb_limit(monkeypatch) -> None:
    """Production and staging send through the app bot: its card body is budgeted
    against 30 KB and captured from the send path."""
    server = _FeishuServer()
    monkeypatch.setattr(alerting, "urlopen", server)
    card = build_feishu_alert_card(
        _huge_payload(60), now=T0, delivery_mode="feishu_app"
    )

    alerting.deliver_feishu_app_card(
        app_id="cli_test", app_secret="s", chat_id="oc_" + "a" * 32, card=card
    )

    token, message = server.requests
    assert token.full_url.endswith("/tenant_access_token/internal")
    assert message.get_header("Content-type") == "application/json; charset=utf-8"
    assert len(message.data) <= FEISHU_BODY_LIMITS["feishu_app"] == 30_000
    sent = json.loads(message.data.decode("utf-8"))
    assert json.loads(sent["content"]) == card
    assert "服务不可用".encode() in message.data
    # the app bot fits more than the webhook would
    assert card_body_bytes(card, "feishu_app") > card_budget("feishu_webhook")


# ---- a page survives its own rendering -------------------------------------------------


@pytest.mark.parametrize(
    ("starts_at", "shown"),
    [
        (1_790_236_800_000, "2026-09-24 16:00（UTC+8）(已持续 10 分钟)"),  # epoch ms
        (1_790_236_800, "2026-09-24 16:00（UTC+8）(已持续 10 分钟)"),
        ("1790236800000", "2026-09-24 16:00（UTC+8）(已持续 10 分钟)"),
        ("9999-12-31T23:59:59Z", "未知"),
        (float("inf"), "未知"),
        (float("nan"), "未知"),
        (-5, "未知"),
        ("not a time", "未知"),
    ],
)
def test_a_malformed_time_is_read_or_unknown_never_a_crash(starts_at, shown) -> None:
    payload = _signoz_payload()
    payload["alerts"][0]["startsAt"] = starts_at

    card = build_feishu_alert_card(payload, now=T0 + 600)
    (item,) = _card_items(card)

    assert "简化卡片" not in card["header"]["title"]["content"]
    assert item["开始于"] == shown


def test_a_render_failure_sends_the_minimal_card_and_logs_it(monkeypatch, caplog):
    """#905 review: any exception while rendering used to drop the page. The minimal
    card still says the level, environment, object and the raw summary."""

    def broken(*_args, **_kwargs):
        raise RuntimeError("renderer bug")

    monkeypatch.setattr(alerting, "_payload_item", broken)
    with caplog.at_level("ERROR", logger="alert-bridge"):
        card = build_feishu_alert_card(_signoz_payload(), now=T0)

    body = "\n".join(e["text"]["content"] for e in card["elements"])
    assert card["header"]["title"]["content"] == (
        "🔴 [P0 告警] FinanceReportHigh5xxRate · 简化卡片"
    )
    assert body.splitlines()[0] == (
        "• P0 · production · finance_report/app · finance-report-backend · "
        "finance_report backend 5xx rate is above 5% for 5 minutes."
    )
    assert "渲染失败 RuntimeError" in body
    assert [r.message for r in caplog.records] == [
        "alert card render failed; sending the minimal card"
    ]


def test_a_card_that_cannot_fit_goes_out_minimal_and_within_the_limit(
    monkeypatch,
) -> None:
    """The final hard cap: when even the smallest plan is over budget, the minimal
    card goes out, itself cut to fit."""
    monkeypatch.setitem(alerting.FEISHU_BODY_LIMITS, "feishu_webhook", 4_000)
    card = build_feishu_alert_card(_huge_payload(60), now=T0)

    body = "\n".join(e["text"]["content"] for e in card["elements"])
    assert card["header"]["title"]["content"].endswith(" · 简化卡片")
    assert card_body_bytes(card, "feishu_webhook") <= card_budget("feishu_webhook")
    assert "…及另外 " in body
    assert "完整卡片超出大小上限" in body


def test_the_title_and_the_links_are_bounded() -> None:
    """#905 review: the alertname, a runbook_url and an externalURL could each carry
    any size; the title is cut and an oversized or non-http link is not used."""
    payload = _signoz_payload()
    payload["commonLabels"]["alertname"] = "X" * 5_000
    payload["alerts"][0]["labels"]["alertname"] = "X" * 5_000
    payload["alerts"][0]["annotations"]["runbook_url"] = (
        "https://a.example/" + "r" * 5_000
    )
    payload["externalURL"] = "https://signoz.example/" + "s" * 5_000

    card = build_feishu_alert_card(payload, now=T0)
    (item,) = _card_items(card)

    assert len(card["header"]["title"]["content"]) < 130
    assert (
        item["Runbook"]
        == f"[ops.observability.md#7-标准操作程序-playbooks]({RUNBOOK_SECTION})"
    )
    assert not [e for e in card["elements"] if e["tag"] == "action"]
    for unsafe in (
        "javascript:alert(1)",
        "https://a.example/x)(y",
        "https://a b.example",
    ):
        payload["alerts"][0]["annotations"]["runbook_url"] = unsafe
        (item,) = _card_items(build_feishu_alert_card(payload, now=T0))
        assert item["Runbook"].endswith(f"({RUNBOOK_SECTION})"), unsafe


def test_a_log_tail_is_redacted_before_it_reaches_feishu() -> None:
    """#905 review: the log tail is shown verbatim, so credentials in it are cut."""
    logs = (
        "connecting to postgresql://app:s3cr3t-pw@db:5432/finance\n"
        "Authorization: Bearer eyJhbGciOi.payload.sig\n"
        "retrying with password=hunter2 and api token=tok-123\n"
    )
    card = build_feishu_alert_card(_breakdown_payload(logs), now=T0)
    (item,) = _card_items(card)
    text = format_signoz_alert(_breakdown_payload(logs), now=T0)

    for secret in ("s3cr3t-pw", "eyJhbGciOi", "hunter2", "tok-123"):
        assert secret not in item["日志"] and secret not in text
    assert "postgresql://***:***@db:5432/finance" in item["日志"]
    assert "Bearer ***" in item["日志"]


def test_the_recovered_section_survives_an_overflow() -> None:
    """#905 review: overflow used to cut the tail of the text, which is where ✅ 已恢复
    sits. Items are dropped from the end of each list instead; both sections keep
    their heading and their count."""
    payload = _huge_payload(15)
    for index in range(15):
        recovered = json.loads(json.dumps(payload["alerts"][index]))
        recovered.update(status="resolved", endsAt="2026-09-24T08:00:00Z")
        recovered["labels"]["service_id"] = f"infra/ok{index}"
        payload["alerts"].append(recovered)

    text = format_signoz_alert(payload, now=T0)
    card = build_feishu_alert_card(payload, now=T0, delivery_mode="feishu_webhook")
    lines = text.splitlines()

    assert len(text) <= MAX_MESSAGE_CHARS and "[truncated]" not in text
    assert "✅ 已恢复 15 项" in lines
    after = lines[lines.index("✅ 已恢复 15 项") + 1 :]
    assert after[-1].startswith("…及另外 ") and after[-1].endswith(" 项")
    assert [line for line in after if line.startswith("• ")]
    assert "**✅ 已恢复 15 项**" in json.dumps(card, ensure_ascii=False)


# ---- pager prose is Chinese (#905) ----------------------------------------------------

#: Product and component names that stay in English wherever they appear.
PRODUCT_WORDS = frozenset(
    {
        "Actions",
        "API",
        "AppRole",
        "BullMQ",
        "CGroup",
        "CI",
        "Cloudflare",
        "Cron",
        "Dokploy",
        "DNS",
        "FIFO",
        "GitHub",
        "HTTP",
        "KV",
        "OOM",
        "Redis",
        "SSH",
        "SigNoz",
        "Vault",
        "VPS",
        "Worker",
        "Workers",
    }
)
_TOKEN = re.compile(r"[A-Za-z0-9_'*%-]+")
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")
_JOIN = re.compile(r"[\s,;:./()]*")


def english_prose(text: str) -> list[str]:
    """Runs of two or more English words outside `code` spans, URLs and product names.

    Pager prose is Chinese (#905); commands, paths and identifiers stay verbatim in
    backticks, evidence is quoted the same way, and neither counts.
    """
    bare = re.sub(r"`[^`]*`", " ", text)
    bare = re.sub(r"https?://\S+", " ", bare)
    runs: list[str] = []
    run: list[str] = []
    end = 0
    for token in _TOKEN.finditer(bare):
        word = _WORD.fullmatch(token.group()) and token.group() not in PRODUCT_WORDS
        if word and run and _JOIN.fullmatch(bare[end : token.start()]):
            run.append(token.group())
        else:
            if len(run) >= 2:
                runs.append(" ".join(run))
            run = [token.group()] if word else []
        end = token.end()
    if len(run) >= 2:
        runs.append(" ".join(run))
    return runs


@pytest.mark.parametrize(
    "prose",
    [
        "reach the VPS over SSH; check the host",
        "heartbeat missing",
        "no detail",
        "dependency unreachable (connection refused)",
        "heartbeat stale: 9000s old (max 5400s)",
        "心跳缺失 · probe loop healthy",
    ],
)
def test_the_english_prose_detector_sees_english(prose) -> None:
    assert english_prose(prose)


@pytest.mark.parametrize(
    "chinese",
    [
        "来源：Cloudflare Workers Cron → 飞书直发(不经 bridge)",
        "在宿主机上执行 `docker compose logs --tail 80`,见 https://a.example/b/c",
        "只用 Dokploy 自己的 `cancel` / `clean`,绝不直接删 Redis / BullMQ 键",
        "依赖不可达(`connection refused`)",
    ],
)
def test_the_english_prose_detector_spares_commands_links_and_products(chinese) -> None:
    assert english_prose(chinese) == []


def test_the_bridge_writes_its_own_prose_in_chinese() -> None:
    """Every impact, next step and cause the in-band sources write is Chinese."""
    from libs.deploy_queue import QUEUE_IMPACT
    from libs.observability.breakdown import BREAKDOWN_PATTERNS, classify_reason

    texts = [
        *alerting._IMPACT_BY_ALERT_OVERRIDE.values(),
        *alerting._IMPACT_BY_DOMAIN.values(),
        DEFAULT_IMPACT,
        *alerting._ACTION_BY_ALERT_OVERRIDE.values(),
        *alerting._ACTION_BY_DOMAIN.values(),
        DEFAULT_ACTION,
        QUEUE_IMPACT,
        *(cause for _marker, cause in BREAKDOWN_PATTERNS),
        classify_reason("")[0],
        classify_reason("some unknown line")[0],
    ]

    assert len(texts) >= 30
    assert {text: english_prose(text) for text in texts if english_prose(text)} == {}


def test_times_are_shown_in_utc_plus_8() -> None:
    """The owner's zone; durations do not depend on it."""
    assert alerting.format_time(T0) == "2026-09-24 16:00（UTC+8）"
    assert alerting.format_time(T0 + 9 * 3600) == "2026-09-25 01:00（UTC+8）"
    assert alerting.since_text(T0, now=T0 + 3 * 86400 + 7200) == (
        "2026-09-24 16:00（UTC+8）(已持续 3 天 2 小时)"
    )
