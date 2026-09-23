"""Infra2 Observability Domain Package."""

from __future__ import annotations

# **按需加载，不在包导入时拉起子模块（审计 A1/A2，2026-09-23）。**
#
# 此前这里是一串 eager import，于是 `import libs.observability` 会连带拉起
# `probes.py`，而后者无条件 `from infra2_sdk.runtime import ...`。后果：
# `libs/watchdog_issue_trail.py` 搬迁后改为经本包取符号，**从纯 stdlib 变成了需要
# infra2-sdk**——而 `ops-checks.yml` 的 watchdog job 只装 `httpx python-dotenv rich`。
#
# 实测（屏蔽 infra2_sdk 后导入）：
#   搬迁前 `libs.watchdog_issue_trail` → OK
#   搬迁后 `libs.watchdog_issue_trail` → ImportError: No module named 'infra2_sdk'
#
# 这条链的终点是 out-of-band watchdog——**别的都挂了之后来叫人的那个**，而且
# 它 schedule-only，没有任何 PR 会触发它，所以这次回归是静默的。
#
# PEP 562 的模块级 `__getattr__`：`from libs.observability import CheckVerdict`
# 仍然可用，但只会拉起 `issue_trail`，不碰 `probes`。需要 SDK 的符号在**被取用时**
# 才要求 SDK，而不是在包被 import 时。
_LAZY: "dict[str, str]" = {
    "Breakdown": "libs.observability.breakdown",
    "BreakdownVerdict": "libs.observability.breakdown",
    "BreakdownWatch": "libs.observability.watchers",
    "CheckVerdict": "libs.observability.issue_trail",
    "ContainerBreakdownWatcher": "libs.observability.watchers",
    "DEFAULT_TIMEOUT_SECONDS": "libs.observability.probes",
    "GitHubIssueClient": "libs.observability.issue_trail",
    "GitHubIssues": "libs.observability.issue_trail",
    "IssueApi": "libs.observability.issue_trail",
    "OPENPANEL_CLIENTS": "libs.observability.openpanel",
    "ProbeSpec": "libs.observability.probes",
    "Trail": "libs.observability.issue_trail",
    "analyze_container_logs": "libs.observability.breakdown",
    "broken_state": "libs.observability.breakdown",
    "build_breakdown_alert_payload": "libs.observability.breakdown",
    "classify_reason": "libs.observability.breakdown",
    "execute_probe": "libs.observability.probes",
    "find_breakdown_containers": "libs.observability.breakdown",
    "find_breakdown_reason": "libs.observability.breakdown",
    "load_trail": "libs.observability.issue_trail",
    "openpanel_env": "libs.observability.openpanel",
    "parse_probe_specs": "libs.observability.probes",
    "reconcile": "libs.observability.issue_trail",
    "reconcile_watchdog_issues": "libs.observability.issue_trail",
    "record_verdicts": "libs.observability.issue_trail",
    "run_probe": "libs.observability.probes",
    "run_probes": "libs.observability.probes",
    "sweep_breakdowns": "libs.observability.watchers",
}

__all__ = sorted(_LAZY)


def __getattr__(name: str):
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(_LAZY[name]), name)
    globals()[name] = value  # 只解析一次
    return value


def __dir__() -> "list[str]":
    return sorted(set(globals()) | set(_LAZY))
