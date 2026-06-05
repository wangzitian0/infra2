"""Out-of-band infra2 host and alert bridge watchdog."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.alerting import deliver_feishu_app_text, deliver_feishu_text  # noqa: E402

DEFAULT_HTTP_TARGETS = """\
infra2-public-entrypoint|https://cloud.zitian.party|200,302
"""

DEFAULT_SSH_TARGETS = """\
infra2-ssh|echo infra2-ssh-ok|infra2-ssh-ok
infra2-docker|docker info >/dev/null && echo docker-ok|docker-ok
infra2-docker-health|sh -lc 'bad="$(docker ps --filter health=unhealthy --filter health=starting --filter status=restarting --format "{{.Names}} {{.Status}}")"; if [ -z "$bad" ]; then echo docker-health-ok; else echo "$bad"; exit 1; fi'|docker-health-ok
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


def run_http_checks(targets: list[HttpTarget], timeout: float) -> list[CheckResult]:
    """Run public endpoint checks from outside infra2."""
    results: list[CheckResult] = []
    for target in targets:
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
            results.append(
                CheckResult(target.name, False, f"GET {target.url} failed: {exc}")
            )
            continue

        if status in target.expected_statuses:
            results.append(CheckResult(target.name, True, f"HTTP {status}"))
        else:
            expected = ",".join(str(code) for code in sorted(target.expected_statuses))
            results.append(
                CheckResult(target.name, False, f"HTTP {status}; expected {expected}")
            )
    return results


def run_ssh_checks(
    config: SshConfig | None, targets: list[SshTarget], timeout: float = 20.0
) -> list[CheckResult]:
    """Run bridge health checks through SSH from the external runner."""
    if not targets:
        return []
    if config is None:
        return [
            CheckResult(target.name, False, "SSH watchdog config is missing")
            for target in targets
        ]

    results: list[CheckResult] = []
    for target in targets:
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
            target.command,
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
            results.append(CheckResult(target.name, False, "ssh command timed out"))
            continue
        except OSError as exc:
            results.append(
                CheckResult(target.name, False, f"ssh command failed: {exc}")
            )
            continue
        output = (completed.stdout + completed.stderr).strip()
        if completed.returncode != 0:
            results.append(
                CheckResult(
                    target.name,
                    False,
                    f"ssh exited {completed.returncode}: {_one_line(output)}",
                )
            )
            continue
        if target.expected_text not in output:
            results.append(
                CheckResult(
                    target.name,
                    False,
                    f"ssh output did not contain expected text: {_one_line(output)}",
                )
            )
            continue
        results.append(
            CheckResult(
                target.name, True, f"ssh output contained {target.expected_text}"
            )
        )
    return results


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
            lines.append(f"- {result.name}: {_redact(result.detail)}")
    return "\n".join(lines)


def main(env: Mapping[str, str] | None = None) -> int:
    """Run watchdog checks and send Feishu directly on failure."""
    current_env = env or os.environ
    http_targets = parse_http_targets(
        current_env.get("INFRA2_WATCHDOG_HTTP_TARGETS", "")
    )
    ssh_targets = parse_ssh_targets(current_env.get("INFRA2_WATCHDOG_SSH_TARGETS", ""))
    timeout = float(current_env.get("INFRA2_WATCHDOG_HTTP_TIMEOUT", "10"))
    ssh_config = load_ssh_config(current_env)

    results = run_http_checks(http_targets, timeout)
    results.extend(run_ssh_checks(ssh_config, ssh_targets))

    for result in results:
        status = "OK" if result.ok else "FAIL"
        print(f"{status} {result.name}: {_redact(result.detail)}")

    failures = [result for result in results if not result.ok]
    if not failures:
        return 0

    message = format_failure_message(failures, run_url=_github_run_url(current_env))
    if current_env.get("WATCHDOG_DRY_RUN") == "1":
        print(message)
        return 1

    deliver_out_of_band_alert(current_env, message)
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


if __name__ == "__main__":
    raise SystemExit(main())
