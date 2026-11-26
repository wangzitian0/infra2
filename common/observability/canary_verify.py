#!/usr/bin/env python3
"""Canary Todo 4-Pillar Touch Reality Physical Verification Tool.

Verifies the physical execution state of Canary Todo (platform/30.todo) across:
  1. Public & Internal Endpoints: /api/health and /api/canary/status
  2. Installed SDK Version: inside container matches locked release
  3. Telemetry (Tracking): SigNoz ClickHouse traces, scoped to the environment
  4. Logs: SigNoz ClickHouse structured logs, scoped to the environment
  5. Monitoring: VPS availability ledger (availability-ledger.json)
  6. Alert Bridge: platform-alerting health status

Usage:
  python -m tools.canary_verify [--env production|staging] [--json]

The trace and log queries come from `common.observability.signoz_queries`. They
filter on the service, on the environment and on the freshness window.

The monitoring pillar judges the ledger on recent failures (#1223). A deploy
restarts the service, and the ledger counts that failed run for the rest of the
UTC day. The pillar therefore passes with failures only when the last failed run
is older than the window and an ok run is inside it. A ledger without the times
(written before #1223) keeps the old rule: any failure fails the pillar.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import shlex
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from common.deploy.vps_ssh import resolve_ssh_credentials as _resolve_ssh_credentials
from common.deploy.vps_ssh import ssh_cmd as _ssh_cmd
from common.observability.signoz_queries import (
    DEFAULT_WINDOW_SECONDS,
    canary_metrics_query,
    gen_ai_metrics_query,
    gen_ai_spans_query,
    logs_query,
    traces_query,
)

TELEMETRY_SERVICE_NAME = "platform-todo"
# The bridge /health status that means healthy. The SDK report says "healthy" (#1209).
BRIDGE_HEALTHY_STATUSES = frozenset({"healthy"})
SSO_PROTECTED_STATUSES = frozenset({301, 302, 303, 307, 308, 401, 403})
# A timestamp may run ahead of the local clock by this much before it counts as skew.
CLOCK_SKEW_TOLERANCE_SECONDS = 60
# Startup grace after container State.StartedAt to absorb deploy restart probe ticks (#1720;
# covers integer-second epoch truncation plus compose.yaml start_period=10s).
RESTART_GRACE_SECONDS = 15


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class PillarResult:
    ok: bool
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class CanaryRealityReport:
    environment: str
    all_ok: bool
    tracking: PillarResult
    logs: PillarResult
    monitoring: PillarResult
    alerts: PillarResult
    installed_sdk: str
    expected_sdk: str = ""
    sdk: PillarResult = field(default_factory=lambda: PillarResult(ok=True, summary=""))
    llm: PillarResult = field(default_factory=lambda: PillarResult(ok=True, summary=""))
    app_peer: PillarResult = field(
        default_factory=lambda: PillarResult(
            ok=True, summary="App-peer checks not sampled"
        )
    )


def _probe_public_summary_sso(
    domain: str, timeout: float = 5.0
) -> tuple[bool, int | str]:
    """Verify unauthenticated POST /api/todos/summary stays protected by SSO."""
    opener = urllib.request.build_opener(_NoRedirectHandler())
    req = urllib.request.Request(
        f"https://{domain}/api/todos/summary",
        data=b"{}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "tools.canary_verify/1.0",
        },
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            code = int(resp.status)
            return code in SSO_PROTECTED_STATUSES, code
    except urllib.error.HTTPError as err:
        return err.code in SSO_PROTECTED_STATUSES, int(err.code)
    except Exception as err:
        return False, str(err)


def _parse_int_tuple(raw: str, expected_len: int) -> list[int]:
    parts = (
        [p.strip() for p in raw.strip().splitlines()[-1].split("\t")]
        if raw.strip()
        else []
    )
    out: list[int] = []
    for idx in range(expected_len):
        val = parts[idx] if idx < len(parts) else "0"
        out.append(int(val) if val.isdigit() else 0)
    return out


def _evaluate_llm_pillar(
    *,
    sections: dict[str, str],
    domain: str,
    require_llm: bool,
) -> PillarResult:
    if "LLM_SUMMARY" not in sections:
        if require_llm:
            return PillarResult(
                ok=False,
                summary="LLM summary section missing from verification output",
            )
        return PillarResult(ok=True, summary="LLM summary not sampled")

    sso_ok, sso_detail = _probe_public_summary_sso(domain)

    def _load_json(name: str) -> dict[str, Any]:
        raw = sections.get(name, "").strip()
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            return {"raw": raw, "error": "invalid JSON"}
        return (
            parsed
            if isinstance(parsed, dict)
            else {"raw": raw, "error": "non-object JSON"}
        )

    non_stream = _load_json("LLM_SUMMARY")
    stream_data = _load_json("LLM_SUMMARY_STREAM")
    invoke_cnt, chat_cnt, aligned_cnt, leak_cnt, cost_cnt = _parse_int_tuple(
        sections.get("GEN_AI_SPANS", ""), 5
    )
    tok_series, dur_series = _parse_int_tuple(sections.get("GEN_AI_METRICS", ""), 2)
    details: dict[str, Any] = {
        "sso_ok": sso_ok,
        "sso_status": sso_detail,
        "non_stream": non_stream,
        "stream": stream_data,
        "spans": {
            "invoke_agent": invoke_cnt,
            "chat": chat_cnt,
            "aligned_attrs": aligned_cnt,
            "content_leaks": leak_cnt,
            "cost_spans": cost_cnt,
        },
        "metrics": {
            "token_usage_series": tok_series,
            "duration_series": dur_series,
        },
    }
    if not sso_ok:
        return PillarResult(
            ok=False,
            summary=f"Public /api/todos/summary unprotected (status={sso_detail})",
            details=details,
        )

    status = non_stream.get("status")
    if status == "unconfigured":
        if require_llm:
            return PillarResult(
                ok=False,
                summary="LLM summary unconfigured (--require-llm set)",
                details=details,
            )
        return PillarResult(
            ok=True,
            summary="LLM summary unconfigured (SSO protected)",
            details=details,
        )

    if status != "pass":
        return PillarResult(
            ok=False,
            summary=f"LLM summary failed: status={status!r}, detail={non_stream.get('detail') or non_stream.get('error') or 'unknown'}",
            details=details,
        )

    problems: list[str] = []
    if stream_data.get("status") != "pass":
        problems.append(f"stream status={stream_data.get('status')!r}")
    if (
        int(stream_data.get("input_tokens", 0) or 0) <= 0
        or int(stream_data.get("output_tokens", 0) or 0) <= 0
    ):
        problems.append("stream usage tokens missing/zero")
    if invoke_cnt < 1 or chat_cnt < 1:
        problems.append(f"GenAI spans missing (invoke={invoke_cnt}, chat={chat_cnt})")
    if aligned_cnt < 1:
        problems.append("GenAI aligned dotted attributes missing")
    if leak_cnt > 0:
        problems.append(f"GenAI span privacy leak detected (leaks={leak_cnt})")
    if cost_cnt < 1:
        problems.append("operation.cost missing on GenAI spans")
    if tok_series < 1 or dur_series < 1:
        problems.append(
            f"GenAI metrics missing in SigNoz (token={tok_series}, duration={dur_series})"
        )

    if problems:
        return PillarResult(ok=False, summary="; ".join(problems), details=details)
    return PillarResult(
        ok=True,
        summary=(
            f"LLM summary passed (stream ok, spans={chat_cnt}, "
            f"cost_spans={cost_cnt}, metrics ok, SSO protected)"
        ),
        details=details,
    )


def _feishu_probe_command(bridge_container: str) -> str:
    script = (
        "import json, urllib.error, urllib.request\n"
        "try:\n"
        "    resp = urllib.request.urlopen('http://127.0.0.1:8080/health/feishu', timeout=10)\n"
        "    print(resp.read().decode())\n"
        "except urllib.error.HTTPError as err:\n"
        "    print(err.read().decode(errors='replace'))\n"
        "except Exception as err:\n"
        "    print(json.dumps({'status': 'error', 'detail': str(err)}))\n"
    )
    script_b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return (
        f"docker exec {bridge_container} python3 -c "
        f"\"import base64; exec(base64.b64decode('{script_b64}').decode())\""
    )


def _summary_probe_command(container: str, *, stream: bool) -> str:
    payload_b64 = base64.b64encode(
        json.dumps({"limit": 5, "stream": stream}).encode("ascii")
    ).decode("ascii")
    script = (
        "import base64, json, urllib.error, urllib.request\n"
        f"req = urllib.request.Request('http://localhost:8000/api/todos/summary', data=base64.b64decode('{payload_b64}'), headers={{'Content-Type': 'application/json'}})\n"
        "try:\n"
        "    print(urllib.request.urlopen(req, timeout=65).read().decode())\n"
        "except urllib.error.HTTPError as err:\n"
        "    print(json.dumps({'status': 'error', 'http_status': err.code, 'detail': err.read().decode(errors='replace')[:300]}))\n"
        "except Exception as err:\n"
        "    print(json.dumps({'status': 'error', 'detail': str(err)}))\n"
    )
    script_b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return (
        f"docker exec {container} python3 -c "
        f"\"import base64; exec(base64.b64decode('{script_b64}').decode())\""
    )


def _parse_canary_sdk_version_from_compose_text(compose_text: str) -> str:
    """SDK version of the Canary image pin (common/deploy/canary_pin.py), or "" (infra2-sdk#247)."""
    from common.deploy.canary_pin import CanaryPinError, canary_image_from_compose_text

    try:
        return canary_image_from_compose_text(compose_text)[1]
    except (CanaryPinError, AttributeError, TypeError):
        return ""


def _sdk_version_of_checkout(root: Path) -> str:
    """The Canary SDK of a checkout: its compose image pin, or "" without one."""
    compose = root / "platform/30.todo/compose.yaml"
    if not compose.exists():
        return ""
    return _parse_canary_sdk_version_from_compose_text(
        compose.read_text(encoding="utf-8")
    )


def _sdk_version_at_ref(root: Path, ref: str, runner) -> tuple[bool, str]:
    """``(found, version)`` of the Canary SDK at ``ref``: the compose image pin."""
    compose = runner(
        ["git", "-C", str(root), "show", f"{ref}:platform/30.todo/compose.yaml"],
        capture_output=True,
        text=True,
    )
    if compose.returncode != 0:
        return False, ""
    return True, _parse_canary_sdk_version_from_compose_text(compose.stdout)


def _resolve_locked_sdk_version(
    *,
    environment: str = "production",
    repo_root: Path | None = None,
    runner=subprocess.run,
) -> str:
    """The SDK version the deployed Canary must run (#1289, infra2-sdk#247).

    It reads the release that the environment serves: the Canary image pin of
    ``platform/30.todo/compose.yaml``. Every release since #247 pins the image there.
    """
    root = repo_root or Path(__file__).resolve().parents[2]
    try:
        from common.deploy.release_markers import (
            PRODUCTION_MARKER_PREFIX,
            newest_release_tag,
            production_marker,
            remote_production_marker,
        )

        if environment == "production":
            remote_ver = remote_production_marker(repo_root=root, runner=runner)
            if remote_ver:
                marker_ref = f"{PRODUCTION_MARKER_PREFIX}{remote_ver}"
                found, version_str = _sdk_version_at_ref(root, marker_ref, runner)
                if not found:
                    fetched = runner(
                        [
                            "git",
                            "-C",
                            str(root),
                            "fetch",
                            "--no-tags",
                            "origin",
                            f"refs/tags/{marker_ref}:refs/tags/{marker_ref}",
                        ],
                        capture_output=True,
                        text=True,
                    )
                    if fetched.returncode == 0:
                        found, version_str = _sdk_version_at_ref(
                            root, marker_ref, runner
                        )
                if not found:
                    found, version_str = _sdk_version_at_ref(root, remote_ver, runner)
                if found and version_str:
                    return version_str
            ref = production_marker(repo_root=root, runner=runner)
        else:
            ref = newest_release_tag(repo_root=root, runner=runner)
        found, version_str = _sdk_version_at_ref(root, ref, runner)
        if found and version_str:
            return version_str
    except Exception:
        pass
    # Production never falls back to the checkout: its pin can be newer than the release
    # that production serves. An empty expected version fails the SDK check.
    if environment == "production":
        return ""
    version_str = _sdk_version_of_checkout(root)
    if version_str:
        return version_str
    try:
        from importlib.metadata import version

        return version("infra2-sdk")
    except Exception:
        return ""


def _as_utc(now: datetime | None) -> datetime:
    """The injected clock, or the real one, as an aware UTC datetime."""
    value = now if now is not None else datetime.now(timezone.utc)
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _evaluate_freshness(
    count: int,
    latest_str: str,
    pillar_name: str,
    max_age_seconds: int = DEFAULT_WINDOW_SECONDS,
    now: datetime | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Fail-closed freshness evaluator for Canary telemetry.

    Validates that:
    1. count > 0
    2. latest_str is non-empty and parses as UTC datetime
    3. age <= max_age_seconds
    4. clock skew is within tolerance (age >= -60s)
    """
    details: dict[str, Any] = {
        "count": count,
        "latest_timestamp": latest_str,
        "max_age_seconds": max_age_seconds,
    }
    if count is None or count <= 0:
        return (
            False,
            f"0 {pillar_name} recorded in the last {max_age_seconds}s",
            details,
        )

    if not latest_str or not str(latest_str).strip():
        return False, f"Missing timestamp for {pillar_name}", details

    raw_ts = str(latest_str).strip()
    clean_ts = raw_ts.split(".")[0]

    try:
        latest = datetime.strptime(clean_ts, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except Exception as err:
        details["parse_error"] = str(err)
        return False, f"Malformed timestamp '{raw_ts}': {err}", details

    now_utc = _as_utc(now)

    age_seconds = (now_utc - latest).total_seconds()
    details["age_seconds"] = age_seconds

    if age_seconds < -CLOCK_SKEW_TOLERANCE_SECONDS:
        return (
            False,
            f"Clock skew detected for {pillar_name}: timestamp {raw_ts} is {int(-age_seconds)}s in future",
            details,
        )

    if age_seconds > max_age_seconds:
        return (
            False,
            f"Stale {pillar_name}: latest entry was at {raw_ts} (age: {int(age_seconds)}s > {max_age_seconds}s)",
            details,
        )

    fresh_label = f"{int(max(0.0, age_seconds))}s ago"
    return (
        True,
        f"{count} {pillar_name} recorded, latest: {raw_ts} (fresh: {fresh_label})",
        details,
    )


def _ledger_count(value: object) -> int | None:
    """A ledger counter or epoch time: a non-negative integer, else None."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _parse_container_started_at(raw: str | None) -> int | None:
    """Parse Docker State.StartedAt RFC3339 timestamp into UTC epoch seconds (#1720)."""
    if not raw:
        return None
    text = raw.strip()
    if not text or text.startswith("0001-01-01"):
        return None
    # Docker emits up to 9 fractional second digits (e.g. .123456789Z); Python <3.11 or
    # strict ISO parsers expect <=6 digits, so drop sub-second fraction (seconds precision).
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, tail = text.split(".", 1)
        tz_pos = next((i for i, ch in enumerate(tail) if ch in "+-"), len(tail))
        text = head + tail[tz_pos:]
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _evaluate_ledger_day(
    ledger_day: object,
    now_utc: datetime,
    *,
    last_ok_epoch: int | None,
    window_seconds: int,
) -> tuple[bool, str]:
    """Require the ledger entry to match today's UTC date (or immediate UTC midnight rollover)."""
    expected_day = now_utc.strftime("%Y-%m-%d")
    day_str = str(ledger_day).strip() if isinstance(ledger_day, str) else ""
    if not day_str:
        return False, f"ledger day missing (expected {expected_day})"
    if day_str == expected_day:
        return True, ""
    yesterday = (now_utc - timedelta(days=1)).strftime("%Y-%m-%d")
    if (
        day_str == yesterday
        and now_utc.hour == 0
        and now_utc.minute < 5
        and last_ok_epoch is not None
        and -CLOCK_SKEW_TOLERANCE_SECONDS
        <= (now_utc.timestamp() - last_ok_epoch)
        <= window_seconds
    ):
        return True, ""
    return False, f"stale ledger day {day_str!r} != {expected_day}"


def _evaluate_ledger_signal(
    signal: object,
    now: datetime,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    container_started_at: int | None = None,
    *,
    ledger_day: object = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Fail-closed judgement of one ledger signal (#1223, #1720).

    The signal passes when it has at least one ok run and either:
    1. it has no failed run,
    2. the last failed run is older than the window and the last ok run is inside it, or
    3. the last failed run occurred at or before the active container's startup grace
       (`container_started_at + RESTART_GRACE_SECONDS`) and a subsequent ok run is inside
       the window (`lastOkAt > lastFailAt`).

    `lastFailAt` and `lastOkAt` are the epoch seconds that the probe runner writes
    (`common.observability.local_ledger`). A signal with failures and a missing or
    malformed time fails the check: the ledger then does not say how old they are.
    """
    details: dict[str, Any] = {"window_seconds": window_seconds}
    if container_started_at is not None:
        details["container_started_at"] = container_started_at
    if ledger_day is not None:
        details["ledger_day"] = ledger_day
        last_ok_for_day = (
            _ledger_count(signal.get("lastOkAt")) if isinstance(signal, dict) else None
        )
        day_ok, day_msg = _evaluate_ledger_day(
            ledger_day,
            _as_utc(now),
            last_ok_epoch=last_ok_for_day,
            window_seconds=window_seconds,
        )
        if not day_ok:
            return False, day_msg, details
    if not isinstance(signal, dict):
        return False, "entry missing for the signal today", details
    ok_count = _ledger_count(signal.get("ok"))
    fail_count = _ledger_count(signal.get("fail", 0))
    if ok_count is None or fail_count is None:
        return False, "counters are not non-negative integers", details
    counts = f"ok={ok_count} fail={fail_count}"
    if ok_count == 0:
        return False, f"{counts}: no ok run recorded", details
    if fail_count == 0:
        return True, counts, details

    last_fail = _ledger_count(signal.get("lastFailAt"))
    last_ok = _ledger_count(signal.get("lastOkAt"))
    if last_fail is None or last_ok is None:
        return (
            False,
            f"{counts}: no valid last fail and last ok time to age the failures",
            details,
        )
    now_ts = now.timestamp()
    fail_age = int(now_ts - last_fail)
    ok_age = int(now_ts - last_ok)
    details["last_fail_age_seconds"] = fail_age
    details["last_ok_age_seconds"] = ok_age
    ages = f"last fail {fail_age}s ago, last ok {ok_age}s ago"
    ok_in_window = -CLOCK_SKEW_TOLERANCE_SECONDS <= ok_age <= window_seconds
    if fail_age <= window_seconds:
        is_restart_failure = (
            container_started_at is not None
            and last_fail <= container_started_at + RESTART_GRACE_SECONDS
            and last_ok > last_fail
            and ok_in_window
        )
        if not is_restart_failure:
            return (
                False,
                f"{counts}: a run failed within the last {window_seconds}s ({ages})",
                details,
            )
        details["ignored_pre_startup_failure"] = True
        return True, f"{counts} ({ages}; ignored deploy-restart failure)", details
    if not ok_in_window:
        return (
            False,
            f"{counts}: no ok run within the last {window_seconds}s ({ages})",
            details,
        )
    return True, f"{counts} ({ages})", details


def _parse_telemetry_pillar(
    section_text: str,
    pillar_name: str,
    count_key: str,
    max_age_seconds: int,
    environment: str,
    now: datetime | None,
) -> PillarResult:
    out = section_text.strip().split("\t")
    count = int(out[0]) if out and out[0].isdigit() else 0
    latest = out[1] if len(out) > 1 else ""
    ok, summary, details = _evaluate_freshness(
        count, latest, pillar_name=pillar_name, max_age_seconds=max_age_seconds, now=now
    )
    details[count_key] = count
    details["environment"] = environment
    details["window_seconds"] = max_age_seconds
    return PillarResult(ok=ok, summary=summary, details=details)


def _version_at_least(raw_version: str, minimum: tuple[int, ...]) -> bool:
    """Return True when raw_version parses cleanly and is >= minimum."""
    parts = [
        part.strip() for part in str(raw_version or "").strip().lstrip("v").split(".")
    ]
    if not parts or any(not part.isdigit() for part in parts):
        return False
    return tuple(int(part) for part in parts) >= minimum


def _split_exit_section(raw: str) -> tuple[int | None, str]:
    """Extract trailing `__EXIT__=<code>` marker and return `(code, body)`."""
    lines = (raw or "").splitlines()
    if lines and lines[-1].startswith("__EXIT__="):
        code_raw = lines[-1].split("=", 1)[1].strip()
        body = "\n".join(lines[:-1]).strip()
        return (int(code_raw) if code_raw.isdigit() else None), body
    return None, (raw or "").strip()


def _evaluate_canary_metrics_pillar(
    section_text: str,
    max_age_seconds: int,
    now: datetime | None,
) -> tuple[bool, str, dict[str, Any]]:
    raw = section_text.strip()
    if not raw:
        return False, "canary metrics section missing", {}
    parts = [p.strip() for p in raw.splitlines()[-1].split("\t")]
    dur_cnt = int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else 0
    out_cnt = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    latest = parts[2] if len(parts) > 2 else ""
    fresh_ok, fresh_summary, details = _evaluate_freshness(
        dur_cnt + out_cnt,
        latest,
        pillar_name="canary metrics",
        max_age_seconds=max_age_seconds,
        now=now,
    )
    details["duration_samples"] = dur_cnt
    details["outcomes_samples"] = out_cnt
    if dur_cnt < 1 or out_cnt < 1:
        return (
            False,
            f"canary metrics incomplete (duration={dur_cnt}, outcomes={out_cnt})",
            details,
        )
    if not fresh_ok:
        return False, fresh_summary, details
    return True, f"pass ({dur_cnt}+{out_cnt} samples)", details


def _evaluate_app_peer_pillar(
    *,
    sections: dict[str, str],
    installed_sdk: str,
    require_app_peer: bool,
) -> PillarResult:
    if "WORKER" not in sections and "SCHEMA_CHECK" not in sections:
        if require_app_peer:
            return PillarResult(
                ok=False,
                summary="App-peer verification sections missing from output",
            )
        return PillarResult(ok=True, summary="App-peer checks not sampled")

    worker_rc, worker_body = _split_exit_section(sections.get("WORKER", ""))
    schema_rc, schema_body = _split_exit_section(sections.get("SCHEMA_CHECK", ""))
    details: dict[str, Any] = {
        "worker_exit": worker_rc,
        "worker_output": worker_body,
        "schema_exit": schema_rc,
        "schema_output": schema_body,
        "installed_sdk": installed_sdk,
    }
    worker_objs: list[dict[str, Any]] = []
    for line in worker_body.splitlines():
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            parsed = json.loads(text)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            worker_objs.append(parsed)
    worker_unsupported = worker_rc == 0 and any(
        obj.get("status") == "unsupported" for obj in worker_objs
    )
    schema_unsupported = schema_rc == 0 and schema_body.strip() == "UNSUPPORTED"
    if worker_unsupported or schema_unsupported:
        return PillarResult(
            ok=False,
            summary="App-peer checks missing or unsupported",
            details=details,
        )

    problems: list[str] = []
    if worker_rc != 0:
        problems.append(f"worker exited {worker_rc}")
    has_worker_log = any(
        obj.get("logger") == "platform-todo-worker"
        or obj.get("service.name") == "platform-todo-worker"
        for obj in worker_objs
    )
    worker_result = next(
        (obj for obj in reversed(worker_objs) if "ok" in obj and "status_code" in obj),
        None,
    )
    if not has_worker_log:
        problems.append("worker init_worker log line missing")
    if (
        worker_result is None
        or worker_result.get("ok") is not True
        or worker_result.get("status_code") != 200
        or worker_result.get("service") not in {"platform/todo", "platform-todo"}
    ):
        problems.append("worker health result missing or failed")

    if schema_rc != 0:
        problems.append(f"schema_check exited {schema_rc}")
    if "ROLLBACK_CLASS:" not in schema_body:
        problems.append("schema_check output missing ROLLBACK_CLASS")

    if problems:
        return PillarResult(ok=False, summary="; ".join(problems), details=details)
    return PillarResult(
        ok=True,
        summary="App-peer worker tick and schema_check passed",
        details=details,
    )


_SAFE_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _evaluate_platform_deps(
    canary_status: dict[str, Any], *, require_platform_deps: bool
) -> tuple[bool, str]:
    unconfigured = canary_status.get("unconfigured") or []
    failed = canary_status.get("failed") or []
    if canary_status.get("ok") is not True or len(failed) > 0:
        return False, "fail"
    if not require_platform_deps:
        return True, "pass"
    if unconfigured:
        return False, f"unconfigured={unconfigured!r}"
    checks = canary_status.get("checks")
    if not isinstance(checks, dict):
        return False, "checks missing"
    gating = canary_status.get("gating") or ("postgres", "redis", "s3")
    for dep in gating:
        entry = checks.get(dep)
        if not isinstance(entry, dict) or entry.get("status") != "pass":
            return False, f"gating {dep} not pass"
    for dep in canary_status.get("informational") or ():
        entry = checks.get(dep)
        if not isinstance(entry, dict) or entry.get("status") == "unconfigured":
            return False, f"informational {dep} unconfigured"
    return True, "pass"


def verify_canary(
    environment: str = "production",
    expected_sdk: str | None = None,
    check_sdk: bool = True,
    max_age_seconds: int = DEFAULT_WINDOW_SECONDS,
    now: datetime | None = None,
    repo_root: Path | None = None,
    require_llm: bool = True,
    require_app_peer: bool = True,
    require_platform_deps: bool = False,
    expected_version: str | None = None,
) -> CanaryRealityReport:
    if expected_sdk is None:
        expected_sdk = _resolve_locked_sdk_version(
            environment=environment, repo_root=repo_root
        )

    host, user, key_path, port = _resolve_ssh_credentials()
    suffix = "" if environment == "production" else "-staging"
    container = f"platform-todo{suffix}"
    bridge_container = f"platform-alerting{suffix}"
    domain = f"todo{suffix}.zitian.party"
    ledger_path = (
        f"/var/lib/infra2-availability-ledger{suffix}/availability-ledger.json"
    )
    signal_id = f"{environment}:todo-canary-status"

    # 1. Public Health Check (runs before ClickHouse SQL construction so observed
    # `service.version` can scope telemetry queries when expected_version is omitted).
    health_ok = False
    health_data: dict[str, Any] = {}
    try:
        req = urllib.request.Request(
            f"https://{domain}/api/health",
            headers={"User-Agent": "tools.canary_verify/1.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                health_data = json.loads(resp.read().decode())
                health_ok = health_data.get("ok") is True
    except Exception as err:
        health_data = {"error": str(err)}

    observed_version = health_data.get("version")
    telemetry_version: str | None = None
    if expected_version and _SAFE_VERSION_RE.fullmatch(expected_version.strip()):
        telemetry_version = expected_version.strip()
    elif (
        health_ok
        and isinstance(observed_version, str)
        and observed_version.strip()
        and observed_version.strip() != "unknown"
        and _SAFE_VERSION_RE.fullmatch(observed_version.strip())
    ):
        telemetry_version = observed_version.strip()

    if expected_version and health_ok:
        if (
            not isinstance(observed_version, str)
            or observed_version.strip() != expected_version.strip()
        ):
            health_ok = False

    # The same environment, version and window scope the ClickHouse telemetry queries.
    # shlex.quote protects the SQL at the remote shell boundary.
    traces_sql = shlex.quote(
        traces_query(
            service_name=TELEMETRY_SERVICE_NAME,
            environment=environment,
            window_seconds=max_age_seconds,
            version=telemetry_version,
        )
    )
    logs_sql = shlex.quote(
        logs_query(
            service_name=TELEMETRY_SERVICE_NAME,
            environment=environment,
            window_seconds=max_age_seconds,
            version=telemetry_version,
        )
    )
    canary_metrics_sql = shlex.quote(
        canary_metrics_query(
            service_name=TELEMETRY_SERVICE_NAME,
            environment=environment,
            window_seconds=max_age_seconds,
            version=telemetry_version,
        )
    )
    gen_ai_spans_sql = shlex.quote(
        gen_ai_spans_query(
            service_name=TELEMETRY_SERVICE_NAME,
            environment=environment,
            window_seconds=max_age_seconds,
            version=telemetry_version,
        )
    )
    gen_ai_metrics_sql = shlex.quote(
        gen_ai_metrics_query(
            service_name=TELEMETRY_SERVICE_NAME,
            environment=environment,
            window_seconds=max_age_seconds,
            version=telemetry_version,
        )
    )

    # 2. In-container Canary Status & Installed SDK via SSH
    llm_non_stream_cmd = _summary_probe_command(container, stream=False)
    llm_stream_cmd = _summary_probe_command(container, stream=True)
    feishu_cmd = _feishu_probe_command(bridge_container)
    remote_script = (
        f'echo "=== STATUS ===" && '
        f"docker exec {container} python3 -c \"import urllib.request, json; print(json.dumps(json.loads(urllib.request.urlopen('http://localhost:8000/api/canary/status').read())))\" && "
        f'echo "=== SDK ===" && '
        f"docker exec {container} python3 -c \"import importlib.metadata; print(importlib.metadata.version('infra2-sdk'))\" && "
        f'echo "=== TRACES ===" && '
        f"docker exec platform-clickhouse clickhouse-client --query {traces_sql} && "
        f'echo "=== LOGS ===" && '
        f"docker exec platform-clickhouse clickhouse-client --query {logs_sql} && "
        f'echo "=== LEDGER ===" && '
        f'jq -c ".days | to_entries | sort_by(.key) | last | {{day: .key, runs: .value.runs, signal: .value.signals[\\"{signal_id}\\"]}}" {ledger_path} && '
        f'echo "=== CANARY_METRICS ===" && '
        f"docker exec platform-clickhouse clickhouse-client --query {canary_metrics_sql} && "
        f'echo "=== CONTAINER_STARTED_AT ===" && '
        f"docker inspect -f '{{{{.State.StartedAt}}}}' {container} && "
        f'echo "=== ALERT_BRIDGE ===" && '
        f"docker exec {bridge_container} python3 -c \"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/health').read().decode())\" && "
        f'echo "=== ALERT_BRIDGE_FEISHU ===" && '
        f"{feishu_cmd} && "
        f'echo "=== LLM_SUMMARY ===" && '
        f"llm_out=$({llm_non_stream_cmd}) && "
        f'printf "%s\\n" "$llm_out" && '
        f'echo "=== LLM_SUMMARY_STREAM ===" && '
        f"{llm_stream_cmd} && "
        f'echo "=== GEN_AI_SPANS ===" && '
        f"gen_ai_spans_out=$(docker exec platform-clickhouse clickhouse-client --query {gen_ai_spans_sql}) && "
        f'case "$llm_out" in *\'"status":"pass"\'*|*\'"status": "pass"\'*) '
        f'for _i in 1 2 3 4 5; do set -- $gen_ai_spans_out; [ "${{1:-0}}" -ge 1 ] 2>/dev/null && [ "${{2:-0}}" -ge 1 ] 2>/dev/null && break; sleep 2; gen_ai_spans_out=$(docker exec platform-clickhouse clickhouse-client --query {gen_ai_spans_sql}); done ;; esac && '
        f'printf "%s\\n" "$gen_ai_spans_out" && '
        f'echo "=== GEN_AI_METRICS ===" && '
        f"gen_ai_metrics_out=$(docker exec platform-clickhouse clickhouse-client --query {gen_ai_metrics_sql}) && "
        f'case "$llm_out" in *\'"status":"pass"\'*|*\'"status": "pass"\'*) '
        f'for _i in 1 2 3 4 5 6 7 8 9 10 11 12 13; do set -- $gen_ai_metrics_out; [ "${{1:-0}}" -ge 1 ] 2>/dev/null && [ "${{2:-0}}" -ge 1 ] 2>/dev/null && break; sleep 5; gen_ai_metrics_out=$(docker exec platform-clickhouse clickhouse-client --query {gen_ai_metrics_sql}); done ;; esac && '
        f'printf "%s\\n" "$gen_ai_metrics_out" && '
        f'echo "=== WORKER ===" && '
        f'{{ worker_out=$(docker exec {container} sh -c \'if [ -f /app/worker.py ]; then python3 /app/worker.py --once --health-url http://127.0.0.1:8000/readyz; else printf "{{\\"status\\":\\"unsupported\\"}}\\n"; fi\' 2>&1); printf "%s\\n__EXIT__=%d\\n" "$worker_out" $?; }} && '
        f'echo "=== SCHEMA_CHECK ===" && '
        f'{{ schema_out=$(docker exec {container} sh -c \'if [ -f /app/canary_schema.py ]; then [ -f /secrets/.env ] && {{ set -a; . /secrets/.env; set +a; }}; if python3 -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)" infra2_sdk.storage >/dev/null 2>&1; then PYTHONPATH="${{PYTHONPATH:+$PYTHONPATH:}}/app" python3 -m infra2_sdk.storage check-schema --metadata canary_schema:METADATA --db-url "$DATABASE_URL" --label platform/todo; else python3 -m infra2_sdk.runtime.schema_check --app-path /app --metadata canary_schema:METADATA --db-url "$DATABASE_URL" --label platform/todo; fi; else printf "UNSUPPORTED\\n"; fi\' 2>&1); printf "%s\\n__EXIT__=%d\\n" "$schema_out" $?; }}'
    )

    code, stdout, stderr = _ssh_cmd(remote_script, host, user, key_path, port)
    if code != 0:
        fail = PillarResult(ok=False, summary=f"SSH execution failed: {stderr.strip()}")
        return CanaryRealityReport(
            environment=environment,
            all_ok=False,
            tracking=fail,
            logs=fail,
            monitoring=fail,
            alerts=fail,
            installed_sdk="unknown",
            expected_sdk=expected_sdk or "",
            sdk=fail,
            llm=fail,
            app_peer=fail,
        )

    sections: dict[str, str] = {}
    current_sec = ""
    for line in stdout.splitlines():
        if line.startswith("=== ") and line.endswith(" ==="):
            current_sec = line.strip("= ")
            sections[current_sec] = ""
        elif current_sec:
            sections[current_sec] = (sections[current_sec] + "\n" + line).strip()

    # Parse Status & Checks
    canary_status: dict[str, Any] = {}
    gating_ok = False
    gating_detail = "fail"
    try:
        parsed_status = json.loads(sections.get("STATUS", "{}"))
        if isinstance(parsed_status, dict):
            canary_status = parsed_status
            gating_ok, gating_detail = _evaluate_platform_deps(
                canary_status, require_platform_deps=require_platform_deps
            )
    except Exception:
        pass

    installed_sdk = sections.get("SDK", "unknown").strip()
    tracking_pillar = _parse_telemetry_pillar(
        sections.get("TRACES", ""),
        "spans",
        "span_count",
        max_age_seconds,
        environment,
        now,
    )
    logs_pillar = _parse_telemetry_pillar(
        sections.get("LOGS", ""),
        "log entries",
        "log_count",
        max_age_seconds,
        environment,
        now,
    )
    metrics_ok, metrics_summary, metrics_details = _evaluate_canary_metrics_pillar(
        sections.get("CANARY_METRICS", ""),
        max_age_seconds,
        now,
    )

    # Parse Monitoring (Ledger)
    ledger_data: dict[str, Any] = {}
    try:
        parsed_ledger = json.loads(sections.get("LEDGER", "{}"))
        if isinstance(parsed_ledger, dict):
            ledger_data = parsed_ledger
    except ValueError:
        pass
    container_started_at = _parse_container_started_at(
        sections.get("CONTAINER_STARTED_AT")
    )
    ledger_ok, ledger_summary, ledger_check = _evaluate_ledger_signal(
        ledger_data.get("signal"),
        _as_utc(now),
        window_seconds=max_age_seconds,
        container_started_at=container_started_at,
        ledger_day=ledger_data.get("day", ""),
    )
    monitoring_ok = ledger_ok and gating_ok and health_ok and metrics_ok

    monitoring_pillar = PillarResult(
        ok=monitoring_ok,
        summary=(
            f"Ledger {ledger_summary}, "
            f"public_health={'pass' if health_ok else 'fail'}, "
            f"internal_status={gating_detail}, "
            f"metrics={'pass' if metrics_ok else metrics_summary}"
        ),
        details={
            "ledger": ledger_data,
            "ledger_check": ledger_check,
            "health": health_data,
            "canary_status": canary_status,
            "canary_metrics": metrics_details,
        },
    )

    # Parse Alert Bridge + /health/feishu
    bridge_ok = False
    bridge_status = "missing"
    bridge_summary = "Bridge response is not valid JSON"
    try:
        bridge_data = json.loads(sections.get("ALERT_BRIDGE", "{}"))
        if isinstance(bridge_data, dict):
            bridge_status = str(bridge_data.get("status", "missing"))
            bridge_ok = bridge_status in BRIDGE_HEALTHY_STATUSES
            bridge_summary = (
                f"Bridge {'healthy' if bridge_ok else 'unhealthy'}: "
                f"status={bridge_status}"
            )
        else:
            bridge_summary = "Bridge response is not a JSON object"
    except ValueError:
        pass

    feishu_ok = False
    feishu_status = "missing"
    feishu_data: dict[str, Any] = {}
    feishu_raw = sections.get("ALERT_BRIDGE_FEISHU", "").strip()
    if feishu_raw:
        try:
            parsed_feishu = json.loads(feishu_raw)
            if isinstance(parsed_feishu, dict):
                feishu_data = parsed_feishu
                feishu_status = str(feishu_data.get("status", "missing"))
                feishu_ok = (
                    feishu_status == "ok" and feishu_data.get("reachable") is True
                )
        except ValueError:
            feishu_status = "invalid JSON"
    if bridge_ok and not feishu_ok:
        bridge_summary = f"Bridge Feishu unhealthy: status={feishu_status}"
    alerts_pillar = PillarResult(
        ok=bridge_ok and feishu_ok,
        summary=bridge_summary,
        details={
            "bridge_response": sections.get("ALERT_BRIDGE", ""),
            "status": bridge_status,
            "feishu": feishu_data,
            "feishu_status": feishu_status,
        },
    )

    # Parse SDK Version against locked requirement
    # Fail closed: without an expected version (no Canary pin found for the served
    # release) the check has nothing to compare and does not pass.
    sdk_ok = True
    if check_sdk:
        sdk_ok = bool(expected_sdk) and installed_sdk == expected_sdk
    if sdk_ok:
        sdk_summary = (
            f"Installed SDK {installed_sdk} matches locked release {expected_sdk}"
        )
    elif not expected_sdk:
        sdk_summary = (
            "Expected SDK unknown: no Canary image pin found for the served release"
        )
    else:
        sdk_summary = f"Installed SDK {installed_sdk} does not match locked release {expected_sdk}"
    sdk_pillar = PillarResult(
        ok=sdk_ok,
        summary=sdk_summary,
        details={"installed_sdk": installed_sdk, "expected_sdk": expected_sdk},
    )

    llm_pillar = _evaluate_llm_pillar(
        sections=sections,
        domain=domain,
        require_llm=require_llm,
    )
    app_peer_pillar = _evaluate_app_peer_pillar(
        sections=sections,
        installed_sdk=installed_sdk,
        require_app_peer=require_app_peer,
    )

    all_ok = (
        tracking_pillar.ok
        and logs_pillar.ok
        and monitoring_pillar.ok
        and alerts_pillar.ok
        and sdk_pillar.ok
        and llm_pillar.ok
        and app_peer_pillar.ok
    )

    return CanaryRealityReport(
        environment=environment,
        all_ok=all_ok,
        tracking=tracking_pillar,
        logs=logs_pillar,
        monitoring=monitoring_pillar,
        alerts=alerts_pillar,
        installed_sdk=installed_sdk,
        expected_sdk=expected_sdk or "",
        sdk=sdk_pillar,
        llm=llm_pillar,
        app_peer=app_peer_pillar,
    )


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {raw!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be greater than 0: {raw!r}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env", choices=("production", "staging"), default="production"
    )
    parser.add_argument(
        "--expected-sdk",
        default=None,
        help="Override the expected SDK version (default: the Canary image pin of the served release)",
    )
    parser.add_argument(
        "--skip-sdk-check",
        action="store_true",
        help="Do not fail verification on SDK version mismatch",
    )
    parser.add_argument(
        "--max-age-seconds",
        type=_positive_int,
        default=DEFAULT_WINDOW_SECONDS,
        help=(
            "Query window and maximum age of the latest traces and logs, in "
            f"seconds (default: {DEFAULT_WINDOW_SECONDS})"
        ),
    )
    parser.add_argument(
        "--require-llm",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Fail verification if LLM summary is unconfigured or unverified (default: true)",
    )
    parser.add_argument(
        "--require-app-peer",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Fail verification if worker tick or schema_check is missing or fails (default: true)",
    )
    parser.add_argument(
        "--require-platform-deps",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Fail verification if any gating dependency is unconfigured or not passing",
    )
    parser.add_argument(
        "--expected-version",
        default=None,
        help="Expected service.version emitted to SigNoz telemetry",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify_canary(
        args.env,
        expected_sdk=args.expected_sdk,
        check_sdk=not args.skip_sdk_check,
        max_age_seconds=args.max_age_seconds,
        require_llm=args.require_llm,
        require_app_peer=args.require_app_peer,
        require_platform_deps=args.require_platform_deps,
        expected_version=args.expected_version,
    )

    if args.json:
        print(json.dumps(asdict(report), indent=2))
    else:
        print(f"=== Canary Todo Reality Verification [{report.environment}] ===")
        print(f"Overall Status: {'PASS' if report.all_ok else 'FAIL'}")
        print(
            f"Installed SDK:  {report.installed_sdk} (expected: {report.expected_sdk or 'any'})"
        )
        print(
            f"1. SDK:         {'[OK]' if report.sdk.ok else '[FAIL]'} {report.sdk.summary}"
        )
        print(
            f"2. Tracking:    {'[OK]' if report.tracking.ok else '[FAIL]'} {report.tracking.summary}"
        )
        print(
            f"3. Logs:        {'[OK]' if report.logs.ok else '[FAIL]'} {report.logs.summary}"
        )
        print(
            f"4. Monitoring:  {'[OK]' if report.monitoring.ok else '[FAIL]'} {report.monitoring.summary}"
        )
        print(
            f"5. Alerts:      {'[OK]' if report.alerts.ok else '[FAIL]'} {report.alerts.summary}"
        )
        print(
            f"6. LLM:         {'[OK]' if report.llm.ok else '[FAIL]'} {report.llm.summary}"
        )
        print(
            f"7. App-Peer:    {'[OK]' if report.app_peer.ok else '[FAIL]'} {report.app_peer.summary}"
        )

    return 0 if report.all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
