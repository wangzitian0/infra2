"""Out-of-band infra2 host and alert bridge watchdog."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.alerting import deliver_feishu_app_text, deliver_feishu_text  # noqa: E402
from libs.dokploy import get_dokploy  # noqa: E402
from libs.dokploy_route_canary import RouteCanaryConfig, run_route_canary  # noqa: E402

DEFAULT_HTTP_TARGETS = """\
infra2-public-entrypoint|https://cloud.zitian.party|200,302
cloudflare-worker-health|https://infra2-cloudflare-watchdog.wangzitian-ai.workers.dev/health|200
"""

DEFAULT_WORKER_STATUS_URL = (
    "https://infra2-cloudflare-watchdog.wangzitian-ai.workers.dev/status"
)

DEFAULT_SSH_TARGETS = """\
infra2-ssh|echo infra2-ssh-ok|infra2-ssh-ok
infra2-docker|docker info >/dev/null && echo docker-ok|docker-ok
infra2-docker-health|sh -lc 'bad="$(docker ps --filter health=unhealthy --filter health=starting --filter status=restarting --format "{{.Names}}")"; if [ -z "$bad" ]; then echo docker-health-ok; else for container in $bad; do docker inspect "$container" --format "name={{.Name}} status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} image={{.Config.Image}}"; done; exit 1; fi'|docker-health-ok
infra2-alert-bridge|docker exec platform-alerting python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=3).read(); print("healthy")'|healthy
"""


@dataclass(frozen=True)
class HttpTarget:
    name: str
    url: str
    expected_statuses: set[int]


@dataclass(frozen=True)
class SshTarget:
    name: str
    command: str
    expected_text: str


@dataclass(frozen=True)
class SshConfig:
    host: str
    user: str
    port: int
    key_path: str


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    failure_domain: str = ""
    attempt_count: int = 1
    duration_ms: int = 0


def parse_http_targets(raw: str) -> list[HttpTarget]:
    """Parse newline-separated `name|url|status_csv` target definitions."""
    targets: list[HttpTarget] = []
    for line in _effective_lines(raw or DEFAULT_HTTP_TARGETS):
        parts = [part.strip() for part in line.split("|")]
        if len(parts) not in (2, 3):
            raise ValueError(f"Invalid HTTP target: {line}")
        expected = _parse_statuses(parts[2] if len(parts) == 3 else "200")
        targets.append(
            HttpTarget(name=parts[0], url=parts[1], expected_statuses=expected)
        )
    return targets


def parse_ssh_targets(raw: str) -> list[SshTarget]:
    """Parse newline-separated `name|command|expected_text` bridge checks."""
    target_by_name: dict[str, SshTarget] = {}
    ordered_names: list[str] = []
    lines = _effective_lines(DEFAULT_SSH_TARGETS)
    if raw:
        lines.extend(_effective_lines(raw))
    for line in lines:
        parts = [part.strip() for part in line.split("|", 2)]
        if len(parts) != 3:
            raise ValueError(f"Invalid SSH target: {line}")
        if parts[0] not in target_by_name:
            ordered_names.append(parts[0])
        target_by_name[parts[0]] = SshTarget(
            name=parts[0], command=parts[1], expected_text=parts[2]
        )
    return [target_by_name[name] for name in ordered_names]


def load_ssh_config(env: Mapping[str, str]) -> SshConfig | None:
    """Return SSH config when every required field is present."""
    host = env.get("INFRA2_WATCHDOG_SSH_HOST", "").strip()
    user = env.get("INFRA2_WATCHDOG_SSH_USER", "").strip()
    key_path = env.get("INFRA2_WATCHDOG_SSH_KEY_PATH", "").strip()
    if not host or not user or not key_path:
        return None
    return SshConfig(
        host=host,
        user=user,
        port=int(env.get("INFRA2_WATCHDOG_SSH_PORT", "") or "22"),
        key_path=key_path,
    )


def run_http_checks(
    targets: list[HttpTarget],
    timeout: float,
    *,
    max_attempts: int = 2,
    retry_delay_seconds: float = 60.0,
) -> list[CheckResult]:
    """Run public endpoint checks from outside infra2 with bounded retries."""
    results: list[CheckResult] = []
    attempts = max(1, max_attempts)
    retry_delay = max(0.0, retry_delay_seconds)
    for target in targets:
        result: CheckResult | None = None
        total_duration_ms = 0
        for attempt in range(1, attempts + 1):
            result = _run_http_check_once(target, timeout)
            total_duration_ms += max(0, result.duration_ms)
            if result.ok:
                detail = result.detail
                if attempt > 1:
                    detail = f"{result.detail}; recovered_on_attempt={attempt}"
                result = CheckResult(
                    result.name,
                    True,
                    detail,
                    result.failure_domain,
                    attempt_count=attempt,
                    duration_ms=total_duration_ms,
                )
                break
            if attempt < attempts and retry_delay > 0:
                time.sleep(retry_delay)
        assert result is not None
        if not result.ok:
            result = CheckResult(
                result.name,
                result.ok,
                result.detail,
                result.failure_domain,
                attempt_count=attempts,
                duration_ms=total_duration_ms,
            )
        results.append(result)
    return results


def run_worker_status_check(
    env: Mapping[str, str],
    timeout: float,
    *,
    max_attempts: int = 2,
    retry_delay_seconds: float = 60.0,
) -> list[CheckResult]:
    """Check authenticated Cloudflare Worker cron/KV-backed watchdog status."""
    url = (
        env.get("INFRA2_WATCHDOG_WORKER_STATUS_URL") or DEFAULT_WORKER_STATUS_URL
    ).strip()
    token = env.get("INFRA2_WATCHDOG_WORKER_STATUS_TOKEN", "").strip()
    if not url:
        return []
    if not token:
        return [
            CheckResult(
                "cloudflare-worker-status",
                False,
                "INFRA2_WATCHDOG_WORKER_STATUS_TOKEN is missing",
                "configuration",
            )
        ]

    attempts = max(1, max_attempts)
    retry_delay = max(0.0, retry_delay_seconds)
    last_results: list[CheckResult] = []
    for attempt in range(1, attempts + 1):
        last_results = _run_worker_status_once(url, token, timeout)
        if all(result.ok for result in last_results):
            return [
                CheckResult(
                    result.name,
                    result.ok,
                    result.detail,
                    result.failure_domain,
                    attempt_count=attempt,
                    duration_ms=result.duration_ms,
                )
                for result in last_results
            ]
        if attempt < attempts and retry_delay > 0:
            time.sleep(retry_delay)
    return [
        CheckResult(
            result.name,
            result.ok,
            result.detail,
            result.failure_domain,
            attempt_count=attempts,
            duration_ms=result.duration_ms,
        )
        for result in last_results
    ]


def _run_worker_status_once(url: str, token: str, timeout: float) -> list[CheckResult]:
    started = time.monotonic()
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "infra2-out-of-band-watchdog/1.0",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            status = response.status
            body = response.read(4096).decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read(512).decode("utf-8", errors="replace")
        return [
            CheckResult(
                "cloudflare-worker-status",
                False,
                f"HTTP {exc.code}; body={_one_line(detail)}",
                "cloudflare-worker-health",
                duration_ms=_elapsed_ms(started),
            )
        ]
    except (OSError, URLError) as exc:
        return [
            CheckResult(
                "cloudflare-worker-status",
                False,
                f"GET {url} failed: {exc}",
                "cloudflare-worker-health",
                duration_ms=_elapsed_ms(started),
            )
        ]

    if status != 200:
        return [
            CheckResult(
                "cloudflare-worker-status",
                False,
                f"HTTP {status}; expected 200",
                "cloudflare-worker-health",
                duration_ms=_elapsed_ms(started),
            )
        ]
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return [
            CheckResult(
                "cloudflare-worker-status",
                False,
                "status response is invalid JSON",
                "cloudflare-worker-health",
                duration_ms=_elapsed_ms(started),
            )
        ]

    last_run = payload.get("lastRun") if isinstance(payload, dict) else {}
    if not isinstance(last_run, dict):
        last_run = {}
    route_count = int(last_run.get("routeTargetCount") or 0)
    heartbeat_count = int(last_run.get("heartbeatTargetCount") or 0)
    age_seconds = last_run.get("ageSeconds")
    last_run_ok = last_run.get("ok")
    failure_count = last_run.get("failureCount")
    delivery_error = last_run.get("deliveryError") or "none"
    if payload.get("ok") is not True:
        return [
            CheckResult(
                "cloudflare-worker-status",
                False,
                (
                    f"worker status unhealthy: age={age_seconds} "
                    f"last_run_ok={last_run_ok} failures={failure_count} "
                    f"routes={route_count} heartbeats={heartbeat_count} "
                    f"delivery_error={_one_line(str(delivery_error))}"
                ),
                "cloudflare-worker-health",
                duration_ms=_elapsed_ms(started),
            )
        ]
    if route_count <= 0 or heartbeat_count <= 0:
        return [
            CheckResult(
                "cloudflare-worker-status",
                False,
                (
                    "worker effective config is empty: "
                    f"routes={route_count} heartbeats={heartbeat_count}"
                ),
                "cloudflare-worker-health",
                duration_ms=_elapsed_ms(started),
            )
        ]
    return [
        CheckResult(
            "cloudflare-worker-status",
            True,
            f"worker last-run fresh: age={age_seconds}s",
            duration_ms=_elapsed_ms(started),
        )
    ]


def _run_http_check_once(target: HttpTarget, timeout: float) -> CheckResult:
    started = time.monotonic()
    request = Request(
        target.url,
        headers={"User-Agent": "infra2-out-of-band-watchdog/1.0"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            status = response.status
    except HTTPError as exc:
        status = exc.code
    except (OSError, URLError) as exc:
        return CheckResult(
            target.name,
            False,
            f"GET {target.url} failed: {exc}",
            _failure_domain_for_http_target(target.name),
            duration_ms=_elapsed_ms(started),
        )

    if status in target.expected_statuses:
        return CheckResult(
            target.name,
            True,
            f"HTTP {status}",
            _failure_domain_for_http_target(target.name),
            duration_ms=_elapsed_ms(started),
        )

    expected = ",".join(str(code) for code in sorted(target.expected_statuses))
    return CheckResult(
        target.name,
        False,
        f"HTTP {status}; expected {expected}",
        _failure_domain_for_http_target(target.name),
        duration_ms=_elapsed_ms(started),
    )


def run_ssh_checks(
    config: SshConfig | None, targets: list[SshTarget], timeout: float = 20.0
) -> list[CheckResult]:
    """Run bridge health checks through SSH from the external runner."""
    if not targets:
        return []
    if config is None:
        return [
            CheckResult(
                target.name,
                False,
                "SSH watchdog config is missing",
                "configuration",
            )
            for target in targets
        ]

    results: list[CheckResult] = []
    for target in targets:
        started = time.monotonic()
        target_command = _decode_ssh_command(target.command)
        command = [
            "ssh",
            "-i",
            config.key_path,
            "-p",
            str(config.port),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            f"{config.user}@{config.host}",
            target_command,
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            results.append(
                CheckResult(
                    target.name,
                    False,
                    "ssh command timed out",
                    _failure_domain_for_ssh_target(target.name),
                    duration_ms=_elapsed_ms(started),
                )
            )
            continue
        except OSError as exc:
            results.append(
                CheckResult(
                    target.name,
                    False,
                    f"ssh command failed: {exc}",
                    _failure_domain_for_ssh_target(target.name),
                    duration_ms=_elapsed_ms(started),
                )
            )
            continue
        output = (completed.stdout + completed.stderr).strip()
        if completed.returncode != 0:
            results.append(
                CheckResult(
                    target.name,
                    False,
                    f"ssh exited {completed.returncode}: {_one_line(output)}",
                    _failure_domain_for_ssh_target(target.name),
                    duration_ms=_elapsed_ms(started),
                )
            )
            continue
        if target.expected_text not in output:
            results.append(
                CheckResult(
                    target.name,
                    False,
                    f"ssh output did not contain expected text: {_one_line(output)}",
                    _failure_domain_for_ssh_target(target.name),
                    duration_ms=_elapsed_ms(started),
                )
            )
            continue
        results.append(
            CheckResult(
                target.name,
                True,
                f"ssh output contained {target.expected_text}",
                _failure_domain_for_ssh_target(target.name),
                duration_ms=_elapsed_ms(started),
            )
        )
    return results


def run_dokploy_route_canary_check(
    env: Mapping[str, str],
    *,
    ssh_config: SshConfig | None,
    runner=run_route_canary,
    client_factory=get_dokploy,
) -> list[CheckResult]:
    """Run the Dokploy route canary as an out-of-band alert signal."""
    started = time.monotonic()
    if not env.get("DOKPLOY_API_KEY", "").strip():
        return [
            CheckResult(
                "infra2-dokploy-route-canary",
                False,
                "DOKPLOY_API_KEY is missing",
                "configuration",
                duration_ms=_elapsed_ms(started),
            )
        ]

    environment_id = env.get("DOKPLOY_ROUTE_CANARY_ENVIRONMENT_ID", "").strip()
    if not environment_id:
        return [
            CheckResult(
                "infra2-dokploy-route-canary",
                False,
                "DOKPLOY_ROUTE_CANARY_ENVIRONMENT_ID is missing",
                "configuration",
                duration_ms=_elapsed_ms(started),
            )
        ]

    run_id = env.get("GITHUB_RUN_ID", "manual").strip() or "manual"
    config = RouteCanaryConfig(
        host=env.get("DOKPLOY_ROUTE_CANARY_HOST", "").strip()
        or "route-canary-watchdog.zitian.party",
        environment_id=environment_id,
        project=env.get("DOKPLOY_ROUTE_CANARY_PROJECT", "").strip() or "platform",
        env=env.get("DOKPLOY_ROUTE_CANARY_ENV", "").strip() or "staging",
        compose_name=env.get("DOKPLOY_ROUTE_CANARY_COMPOSE_NAME", "").strip()
        or "dokploy-route-canary-watchdog",
        nonce=run_id,
        timeout_seconds=int(
            env.get("DOKPLOY_ROUTE_CANARY_TIMEOUT_SECONDS", "") or "180"
        ),
        interval_seconds=int(
            env.get("DOKPLOY_ROUTE_CANARY_INTERVAL_SECONDS", "") or "5"
        ),
        ssh_host=ssh_config.host if ssh_config else "",
        ssh_user=ssh_config.user if ssh_config else "root",
        ssh_port=ssh_config.port if ssh_config else 22,
        ssh_key_path=ssh_config.key_path if ssh_config else "",
        repair_stale_compose=True,
    )
    try:
        report = runner(
            config,
            client_factory(
                host=env.get("DOKPLOY_ROUTE_CANARY_DOKPLOY_HOST", "").strip()
                or "cloud.zitian.party"
            ),
        )
    except Exception as exc:  # noqa: BLE001 - watchdog must turn exceptions into alerts.
        return [
            CheckResult(
                "infra2-dokploy-route-canary",
                False,
                f"canary raised {type(exc).__name__}: {_one_line(str(exc))}",
                "dokploy-control-plane",
                duration_ms=_elapsed_ms(started),
            )
        ]

    detail = (
        f"status={report.status} failure_domain={report.failure_domain or 'none'} "
        f"compose_id={report.compose_id or 'unknown'} public_url={report.public_url}"
    )
    if report.status == "pass":
        return [
            CheckResult(
                "infra2-dokploy-route-canary",
                True,
                detail,
                duration_ms=_elapsed_ms(started),
            )
        ]

    if report.steps:
        failed_steps = [
            f"{step.name}:{step.status}:{_one_line(step.detail)}"
            for step in report.steps
            if step.status != "pass"
        ]
        if failed_steps:
            detail = f"{detail} failed_steps={' ; '.join(failed_steps)}"
    return [
        CheckResult(
            "infra2-dokploy-route-canary",
            False,
            detail,
            report.failure_domain or "dokploy-control-plane",
            duration_ms=_elapsed_ms(started),
        )
    ]


def format_failure_message(results: list[CheckResult], *, run_url: str) -> str:
    """Build a Feishu message for failed out-of-band checks."""
    lines = [
        "[OUT-OF-BAND] Infra2 watchdog failed",
        "Severity: P0",
        "Scope: host reachability / alert bridge availability",
        "Route: GitHub Actions -> Feishu direct",
    ]
    if run_url:
        lines.append(f"Run: {run_url}")
    lines.append("Failures:")
    for result in results:
        if not result.ok:
            domain = f"[{result.failure_domain}] " if result.failure_domain else ""
            lines.append(f"- {domain}{result.name}: {_redact(result.detail)}")
            lines.append(
                f"  Action: {_suggested_action_for_failure(result.name, result.failure_domain)}"
            )
            lines.append(
                f"  Runbook: {_runbook_url_for_failure(result.failure_domain)}"
            )
    return "\n".join(lines)


def main(env: Mapping[str, str] | None = None) -> int:
    """Run watchdog checks and send Feishu directly on failure."""
    run_started = time.monotonic()
    current_env = env or os.environ
    http_targets = parse_http_targets(
        current_env.get("INFRA2_WATCHDOG_HTTP_TARGETS", "")
    )
    ssh_targets = parse_ssh_targets(current_env.get("INFRA2_WATCHDOG_SSH_TARGETS", ""))
    timeout = float(current_env.get("INFRA2_WATCHDOG_HTTP_TIMEOUT", "10"))
    retry_max_attempts = int(current_env.get("INFRA2_WATCHDOG_RETRY_MAX_ATTEMPTS", "2"))
    retry_delay_seconds = float(
        current_env.get("INFRA2_WATCHDOG_RETRY_DELAY_SECONDS", "60")
    )
    ssh_config = load_ssh_config(current_env)

    _emit_structured_log(
        {
            "event": "watchdog.run.start",
            "timeout_seconds": timeout,
            "retry_max_attempts": max(1, retry_max_attempts),
            "retry_delay_seconds": max(0.0, retry_delay_seconds),
            "http_target_count": len(http_targets),
            "ssh_target_count": len(ssh_targets),
        }
    )

    results = run_http_checks(
        http_targets,
        timeout,
        max_attempts=retry_max_attempts,
        retry_delay_seconds=retry_delay_seconds,
    )
    results.extend(
        run_worker_status_check(
            current_env,
            timeout,
            max_attempts=retry_max_attempts,
            retry_delay_seconds=retry_delay_seconds,
        )
    )
    results.extend(run_dokploy_route_canary_check(current_env, ssh_config=ssh_config))
    results.extend(run_ssh_checks(ssh_config, ssh_targets))
    for result in results:
        _emit_structured_log(
            {
                "event": "watchdog.check",
                "name": result.name,
                "status": "ok" if result.ok else "fail",
                "failure_domain": result.failure_domain,
                "attempt_count": result.attempt_count,
                "duration_ms": result.duration_ms,
                "detail": _redact(result.detail),
            }
        )
        status = "OK" if result.ok else "FAIL"
        print(f"{status} {result.name}: {_redact(result.detail)}")

    failures = [result for result in results if not result.ok]
    if not failures:
        _emit_structured_log(
            {
                "event": "watchdog.run.complete",
                "status": "ok",
                "failure_count": 0,
                "run_duration_ms": _elapsed_ms(run_started),
            }
        )
        return 0

    message = format_failure_message(failures, run_url=_github_run_url(current_env))
    if current_env.get("WATCHDOG_DRY_RUN") == "1":
        print(message)
        _emit_structured_log(
            {
                "event": "watchdog.run.complete",
                "status": "fail",
                "failure_count": len(failures),
                "dry_run": True,
                "run_duration_ms": _elapsed_ms(run_started),
            }
        )
        return 1

    try:
        deliver_out_of_band_alert(current_env, message)
    except Exception as exc:  # noqa: BLE001 - watchdog must not fail silently.
        fallback_issue_url = create_delivery_fallback_issue(
            current_env, failures=failures, error=str(exc)
        )
        _emit_structured_log(
            {
                "event": "watchdog.delivery.failure",
                "status": "fail",
                "failure_count": len(failures),
                "error": _redact(_one_line(str(exc))),
                "fallback_issue_url": fallback_issue_url or "",
                "run_duration_ms": _elapsed_ms(run_started),
            }
        )
        return 1
    _emit_structured_log(
        {
            "event": "watchdog.run.complete",
            "status": "fail",
            "failure_count": len(failures),
            "dry_run": False,
            "run_duration_ms": _elapsed_ms(run_started),
        }
    )
    return 1


def deliver_out_of_band_alert(env: Mapping[str, str], message: str) -> None:
    """Send a direct Feishu alert without using the infra2 bridge."""
    mode = (
        env.get("INFRA2_OUT_OF_BAND_ALERT_DELIVERY_MODE")
        or env.get("ALERT_DELIVERY_MODE")
        or "feishu_webhook"
    ).strip()
    if mode == "feishu_app":
        deliver_feishu_app_text(
            app_id=env.get("INFRA2_OUT_OF_BAND_FEISHU_APP_ID")
            or env.get("FEISHU_APP_ID", ""),
            app_secret=env.get("INFRA2_OUT_OF_BAND_FEISHU_APP_SECRET")
            or env.get("FEISHU_APP_SECRET", ""),
            chat_id=env.get("INFRA2_OUT_OF_BAND_FEISHU_CHAT_ID")
            or env.get("FEISHU_CHAT_ID", ""),
            api_base=env.get("INFRA2_OUT_OF_BAND_FEISHU_API_BASE")
            or env.get("FEISHU_API_BASE", "https://open.feishu.cn"),
            text=message,
        )
        return

    if mode != "feishu_webhook":
        raise ValueError(f"Unsupported out-of-band delivery mode: {mode}")

    webhook_url = (
        env.get("INFRA2_OUT_OF_BAND_FEISHU_WEBHOOK_URL")
        or env.get("FEISHU_WEBHOOK_URL")
        or ""
    )
    if not webhook_url:
        raise ValueError(
            "INFRA2_OUT_OF_BAND_FEISHU_WEBHOOK_URL or FEISHU_WEBHOOK_URL "
            "is required for feishu_webhook mode"
        )
    deliver_feishu_text(webhook_url, message)


def _failure_domain_for_http_target(name: str) -> str:
    if name == "cloudflare-worker-health":
        return "cloudflare-worker-health"
    if name == "infra2-public-entrypoint":
        return "host-reachability"
    if name.endswith("public-route"):
        return "public-route"
    return "http-target"


def _failure_domain_for_ssh_target(name: str) -> str:
    if name == "infra2-ssh":
        return "host-reachability"
    if name in {"infra2-docker", "infra2-docker-health"}:
        return "docker-runtime"
    if name == "infra2-alert-bridge":
        return "alert-bridge"
    return "host-diagnostics"


def _suggested_action_for_failure(name: str, failure_domain: str) -> str:
    if failure_domain == "host-reachability":
        return "verify VPS reachability and DNS from an external network (curl + traceroute)"
    if failure_domain == "docker-runtime":
        return "SSH into infra2 and run `docker ps` / `docker inspect` for unhealthy containers"
    if failure_domain == "alert-bridge":
        return (
            "check `platform-alerting` container logs and /health from inside the host"
        )
    if failure_domain == "cloudflare-worker-health":
        return "call worker /status with WATCHDOG_STATUS_TOKEN and verify last-run freshness"
    if (
        failure_domain == "dokploy-control-plane"
        or name == "infra2-dokploy-route-canary"
    ):
        return "check Dokploy API and route canary deploy logs for rollout/network failures"
    return "inspect the failed check detail and verify target service health manually"


def _runbook_url_for_failure(failure_domain: str) -> str:
    anchor = "#out-of-band-watchdog"
    if failure_domain == "alert-bridge":
        anchor = "#alerting-bridge"
    elif failure_domain in {"host-reachability", "docker-runtime"}:
        anchor = "#verification"
    return (
        "https://github.com/wangzitian0/infra2/blob/main/platform/12.alerting/README.md"
        f"{anchor}"
    )


def create_delivery_fallback_issue(
    env: Mapping[str, str], *, failures: list[CheckResult], error: str
) -> str | None:
    """Create a GitHub issue fallback when Feishu delivery fails."""
    enabled = env.get("INFRA2_WATCHDOG_ENABLE_FALLBACK_ISSUE", "1").strip().lower()
    if enabled in {"0", "false", "no"}:
        return None
    token = env.get("GITHUB_TOKEN", "").strip()
    repository = env.get("GITHUB_REPOSITORY", "").strip()
    if not token or not repository or "/" not in repository:
        return None

    owner, repo = repository.split("/", 1)
    run_url = _github_run_url(env)
    now_iso = datetime.now(UTC).isoformat()
    labels = [
        env.get("INFRA2_WATCHDOG_FALLBACK_ISSUE_LABEL", "watchdog-alert-fallback")
    ]
    body_lines = [
        "Feishu delivery failed for out-of-band watchdog alert.",
        f"- Time: {now_iso}",
        f"- Error: {_redact(_one_line(error))}",
    ]
    if run_url:
        body_lines.append(f"- Run: {run_url}")
    body_lines.append("")
    body_lines.append("Failed checks:")
    for failure in failures:
        body_lines.append(
            f"- [{failure.failure_domain or 'unknown'}] {failure.name}: {_redact(_one_line(failure.detail))}"
        )
    payload = {
        "title": "[watchdog-alert-fallback] out-of-band delivery failure",
        "body": "\n".join(body_lines),
        "labels": labels,
    }
    request = Request(
        f"https://api.github.com/repos/{owner}/{repo}/issues",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "infra2-out-of-band-watchdog/1.0",
            "Content-Type": "application/json",
        },
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:  # noqa: S310
            response_body = json.loads(response.read().decode("utf-8"))
    except (HTTPError, OSError, URLError, json.JSONDecodeError):
        return None
    html_url = response_body.get("html_url")
    return str(html_url) if isinstance(html_url, str) and html_url else None


def _effective_lines(raw: str) -> list[str]:
    return [
        line.strip()
        for line in raw.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _parse_statuses(raw: str) -> set[int]:
    statuses = {int(value.strip()) for value in raw.split(",") if value.strip()}
    if not statuses:
        raise ValueError("At least one expected HTTP status is required")
    return statuses


def _decode_ssh_command(command: str) -> str:
    if not command.startswith("base64:"):
        return command
    return base64.b64decode(command.removeprefix("base64:")).decode("utf-8")


def _github_run_url(env: Mapping[str, str]) -> str:
    server = env.get("GITHUB_SERVER_URL", "")
    repo = env.get("GITHUB_REPOSITORY", "")
    run_id = env.get("GITHUB_RUN_ID", "")
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return ""


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _redact(value: str) -> str:
    redacted = re.sub(
        r"https://open\.(?:feishu\.cn|larksuite\.com)/open-apis/bot/v2/hook/[^\s]+",
        "https://open.feishu.cn/open-apis/bot/v2/hook/***",
        value,
    )
    redacted = re.sub(
        r"(?i)(secret|token|password)[A-Za-z0-9._:/=-]*",
        r"\1=***",
        redacted,
    )
    return redacted


def _emit_structured_log(payload: Mapping[str, object]) -> None:
    body = dict(payload)
    body.setdefault("timestamp", int(time.time()))
    print(json.dumps(body, ensure_ascii=True, sort_keys=True))


def _elapsed_ms(started: float) -> int:
    return int(max(0.0, time.monotonic() - started) * 1000)


if __name__ == "__main__":
    raise SystemExit(main())
