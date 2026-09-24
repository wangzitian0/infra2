"""Out-of-band infra2 watchdog: the daily GitHub audit (ops.observability.md §1.1).

It pages the failure classes the GitHub layer owns -- the Cloudflare Worker's
liveness (runs and delivers), backups and the restore rehearsal, the peer
scheduler -- plus failures of its own configuration and any check the signal
registry does not mark `type: report`. The report-only checks (host, Docker,
bridge, Dokploy status, staging backups) still run, and their failures go to one
daily report because another layer pages them (#908).
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.alerting import (  # noqa: E402
    INFRA2_REPORTS_ENV,
    REPORT_TITLE_PREFIX,
    PagerItem,
    PagerMessage,
    deliver_feishu_app_text,
    deliver_feishu_text,
    deliver_infra2_report,
    firing_title,
    format_utc,
    highest_level,
    pager_level,
    render_pager_text,
)
from libs.deploy_queue import deployment_start_epoch  # noqa: E402
from libs.dokploy import get_dokploy  # noqa: E402
from libs.scheduler_peer_liveness import (  # noqa: E402
    BOUND_CAP_ENV,
    OK as PEER_OK,
    UNVERIFIABLE,
    evaluate as evaluate_peer_liveness,
    github_getter,
    parse_bound_cap_hours,
)
from libs.watchdog_issue_trail import (  # noqa: E402
    VERDICTS_ENV,
    WATCHDOG_SOURCE,
    CheckVerdict,
    record_verdicts,
)

#: #908: the Worker's `/health` always answers `{"ok": true}`, so it could not
#: detect anything; the Worker's liveness is judged from `/status` freshness.
DEFAULT_HTTP_TARGETS = """\
infra2-public-entrypoint|https://cloud.zitian.party|200,302
"""

DEFAULT_WORKER_STATUS_URL = (
    "https://infra2-cloudflare-watchdog.wangzitian-ai.workers.dev/status"
)
WORKER_STATUS_CHECK = "cloudflare-worker-status"
#: The Worker's own staleness bound (`WATCHDOG_STATUS_MAX_AGE_SECONDS`) lives in
#: its deploy config; the GitHub audit reads the same value instead of a copy.
WORKER_WRANGLER = ROOT / "cloudflare/infra-watchdog/wrangler.toml"
WORKER_MAX_AGE_VAR = "WATCHDOG_STATUS_MAX_AGE_SECONDS"
#: worker.js statusResponse: a last run this far "in the future" is clock skew, stale.
WORKER_FUTURE_TOLERANCE_SECONDS = 300

#: #908 (ops.observability.md §1.1): which checks page is decided by the signal
#: registry, not here. A GitHub signal pages unless it is `type: report`; the
#: consistency audit fails a paging signal outside its failure class's pager layer.
SIGNAL_REGISTRY = ROOT / "docs/ssot/watchdog-signals.yaml"
SIGNAL_REGISTRY_CHECK = "watchdog-signal-registry"
REPORT_CHECK = "watchdog-report-delivery"
#: Failures of the watchdog itself. They page whichever check they surfaced on:
#: a watchdog that cannot see is silent, and only the pager is sure to be read.
WATCHDOG_SELF_DOMAINS = frozenset({"configuration", "report-delivery"})
#: Checks that exist only to report the watchdog's own failure. The issue trail
#: records them as the watchdog step's own verdict, which a clean run closes.
WATCHDOG_SELF_CHECKS = frozenset({SIGNAL_REGISTRY_CHECK, REPORT_CHECK})
PAGE, REPORT = "page", "report"

#: truealpha#876: the peer that sees truealpha's scheduler-liveness workflow die.
PEER_SCHEDULER_LIVENESS_CHECK = "truealpha-scheduler-liveness"
DOKPLOY_STATUS_CHECK = "infra2-dokploy-status"
DOKPLOY_STATUS_FAMILY = "dokploy-status:"
#: #908: a Dokploy `error` whose latest deployment is older than this is labelled
#: an old record in the report. Age alone never makes it stale: only per-unit
#: runtime evidence does (the unit's own containers run healthy on the current
#: config), because an old version can keep running healthy after a failed pull.
DOKPLOY_RECORD_MAX_AGE_HOURS = 72
#: Every config-bound app container carries the IAC_CONFIG_HASH it was created
#: with, and Dokploy's compose env holds the current one (ops.pipeline.md).
CONFIG_HASH_KEY = "IAC_CONFIG_HASH"
#: `docker inspect` line per container of one unit. Only IAC_CONFIG_HASH is taken
#: from the environment; no other variable leaves the host.
UNIT_RUNTIME_FORMAT = (
    "{{.Name}}|{{.State.Status}}|{{.State.ExitCode}}|"
    "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|"
    '{{range .Config.Env}}{{if eq (index (split . "=") 0) "IAC_CONFIG_HASH"}}'
    '{{index (split . "=") 1}}{{end}}{{end}}'
)
STATE_DISCREPANCY_RUNBOOK = (
    "https://github.com/wangzitian0/infra2/blob/main/"
    "docs/ssot/ops.standards.md#rule-4-"
    "状态不一致协议-state-discrepancy-protocol"
)

DEFAULT_SSH_TARGETS = """\
infra2-ssh|echo infra2-ssh-ok|infra2-ssh-ok
infra2-docker|docker info >/dev/null && echo docker-ok|docker-ok
infra2-docker-health|sh -lc 'bad="$(docker ps --filter health=unhealthy --format "{{.Names}}"; docker ps --filter health=starting --format "{{.Names}}"; docker ps --filter status=restarting --format "{{.Names}}"; docker ps -a --filter status=created --format "{{.Names}}"; docker ps -a --filter status=exited --format "{{.Names}}")"; seen=""; flagged=""; for container in $bad; do case " $seen " in *" $container "*) continue;; esac; seen="$seen $container"; line="$(docker inspect "$container" --format "name={{.Name}} status={{.State.Status}} exit_code={{.State.ExitCode}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} image={{.Config.Image}}")"; status="$(docker inspect "$container" --format "{{.State.Status}}")"; code="$(docker inspect "$container" --format "{{.State.ExitCode}}")"; if [ "$status" = "exited" ] && [ "$code" = "0" ]; then continue; fi; flagged="$flagged\\n$line"; done; if [ -z "$flagged" ]; then echo docker-health-ok; else printf "%b" "$flagged"; echo; exit 1; fi'|docker-health-ok
infra2-alert-bridge|docker exec platform-alerting python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=3).read(); print("healthy")'|healthy
"""

#: #895: the weekly off-host backups (SOP-006) and the restore rehearsal
#: (SOP-006A) only wrote host log files, so a failed or skipped run reached no one
#: (#618 went unnoticed for weeks; the rehearsal cron in #892 had never run).
BACKUP_CHECKS = (
    ("infra2-backup-production", "production"),
    ("infra2-backup-staging", "staging"),
)
RESTORE_REHEARSAL_CHECK = "infra2-restore-rehearsal"
#: Where the rehearsal cron appends its output (crontab in ops.recovery.md SOP-006A).
RESTORE_REHEARSAL_LOG = "/var/log/infra2-backup-restore-rehearsal.log"
#: The crontab that holds that cron (root's). Read only while the log is missing
#: (#926): its entry separates "not scheduled" from "not yet due", and its mtime
#: is when the schedule was last written.
RESTORE_REHEARSAL_CRONTAB = "/var/spool/cron/crontabs/root"
#: ssh's own exit status when it cannot connect or authenticate.
SSH_TRANSPORT_FAILURE = 255


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
    severity: str = "P1"
    #: Information for the daily report that is not this check's verdict (#908),
    #: e.g. the Worker's own last-run findings.
    note: str = ""
    #: Epoch seconds of the evidence behind a Dokploy `error` (its latest deployment).
    recorded_at: float | None = None
    #: Per-unit proof that a Dokploy `error` is stale: the unit's own containers run
    #: healthy on the current config. Empty means no such proof.
    runtime_evidence: str = ""


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
        for attempt in range(1, attempts + 1):
            result = _run_http_check_once(target, timeout)
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
            )
        results.append(result)
    return results


def run_worker_status_check(
    env: Mapping[str, str],
    timeout: float,
    *,
    max_attempts: int = 2,
    retry_delay_seconds: float = 60.0,
    max_age_seconds: int | None = None,
) -> list[CheckResult]:
    """Is the Cloudflare Worker running and delivering? (watchdog-liveness, #908)

    Red when its last run is older than the Worker's own
    `WATCHDOG_STATUS_MAX_AGE_SECONDS`, or when that run could not deliver its
    alerts. What a run found and did deliver (failures, an empty config) the
    Worker already paged; here it is a note for the daily report, so one Worker
    finding is not paged a second time a day later.
    """
    url = (
        env.get("INFRA2_WATCHDOG_WORKER_STATUS_URL") or DEFAULT_WORKER_STATUS_URL
    ).strip()
    token = env.get("INFRA2_WATCHDOG_WORKER_STATUS_TOKEN", "").strip()
    if not url:
        return []
    if not token:
        return [
            CheckResult(
                WORKER_STATUS_CHECK,
                False,
                "INFRA2_WATCHDOG_WORKER_STATUS_TOKEN is missing",
                "configuration",
            )
        ]
    if max_age_seconds is None:
        try:
            max_age_seconds = worker_status_max_age_seconds()
        except Exception as exc:  # noqa: BLE001 - an unknown bound is red, never a pass.
            return [
                CheckResult(
                    WORKER_STATUS_CHECK,
                    False,
                    f"cannot read {WORKER_MAX_AGE_VAR} from {WORKER_WRANGLER.name}: "
                    f"{type(exc).__name__}: {_one_line(str(exc))}",
                    "configuration",
                )
            ]

    attempts = max(1, max_attempts)
    retry_delay = max(0.0, retry_delay_seconds)
    result = None
    for attempt in range(1, attempts + 1):
        result = replace(
            _run_worker_status_once(url, token, timeout, max_age_seconds),
            attempt_count=attempt,
        )
        if result.ok:
            break
        if attempt < attempts and retry_delay > 0:
            time.sleep(retry_delay)
    assert result is not None
    return [result]


def worker_status_max_age_seconds(path: Path = WORKER_WRANGLER) -> int:
    """The Worker's configured staleness bound, read from its wrangler.toml."""
    import tomllib

    raw = tomllib.loads(path.read_text(encoding="utf-8"))["vars"][WORKER_MAX_AGE_VAR]
    value = int(str(raw).strip())
    if value <= 0:
        raise ValueError(f"{WORKER_MAX_AGE_VAR}={raw!r} is not a positive bound")
    return value


def _run_worker_status_once(
    url: str, token: str, timeout: float, max_age_seconds: int
) -> CheckResult:
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "infra2-out-of-band-watchdog/1.0",
        },
        method="GET",
    )

    def red(detail: str) -> CheckResult:
        return CheckResult(
            WORKER_STATUS_CHECK, False, detail, "cloudflare-worker-health"
        )

    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            status = response.status
            body = response.read(4096).decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read(512).decode("utf-8", errors="replace")
        return red(f"HTTP {exc.code}; body={_one_line(detail)}")
    except (OSError, URLError) as exc:
        return red(f"GET {url} failed: {exc}")
    if status != 200:
        return red(f"HTTP {status}; expected 200")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return red("status response is invalid JSON")

    last_run = payload.get("lastRun") if isinstance(payload, dict) else None
    if not isinstance(last_run, dict):
        last_run = {}
    age = last_run.get("ageSeconds")
    if not isinstance(age, (int, float)) or isinstance(age, bool):
        return red(f"worker has no recorded run: ageSeconds={age!r}")
    if age < -WORKER_FUTURE_TOLERANCE_SECONDS:
        return red(f"worker last run is {-age}s in the future (clock skew?)")
    if age > max_age_seconds:
        return red(f"worker last run is stale: age={age}s > max {max_age_seconds}s")
    delivery_error = _one_line(str(last_run.get("deliveryError") or ""))
    if delivery_error:
        # Liveness is "runs AND delivers": a Worker whose pages do not arrive is as
        # blind as a stopped one, and nothing else in the Worker can say so.
        return red(
            f"worker runs but could not deliver its alerts: age={age}s "
            f"failures={last_run.get('failureCount') or 0} "
            f"delivery_error={delivery_error}"
        )
    return CheckResult(
        WORKER_STATUS_CHECK,
        True,
        f"worker last run fresh: age={age}s (max {max_age_seconds}s)",
        note=_worker_delivered_findings_note(last_run),
    )


def _worker_delivered_findings_note(last_run: Mapping[str, object]) -> str:
    """What a fresh, delivering Worker run found, for the report; empty when clean.

    The Worker paged these itself, so here they are information, not a page.
    """
    failure_count = last_run.get("failureCount") or 0
    # worker.js: `ok: lastRun.ok !== false`, so only an explicit False is a failure.
    if last_run.get("ok") is not False and not failure_count:
        return ""
    return (
        f"Cloudflare Worker's last run found failures, delivered by the Worker "
        f"itself: ok={last_run.get('ok')} failures={failure_count} "
        f"routes={last_run.get('routeTargetCount')} "
        f"heartbeats={last_run.get('heartbeatTargetCount')}"
    )


def _run_http_check_once(target: HttpTarget, timeout: float) -> CheckResult:
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
        )

    if status in target.expected_statuses:
        return CheckResult(
            target.name,
            True,
            f"HTTP {status}",
            _failure_domain_for_http_target(target.name),
        )

    expected = ",".join(str(code) for code in sorted(target.expected_statuses))
    return CheckResult(
        target.name,
        False,
        f"HTTP {status}; expected {expected}",
        _failure_domain_for_http_target(target.name),
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
        command = _ssh_argv(config, _decode_ssh_command(target.command))
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
                )
            )
            continue
        results.append(
            CheckResult(
                target.name,
                True,
                f"ssh output contained {target.expected_text}",
                _failure_domain_for_ssh_target(target.name),
            )
        )
    return results


def _ssh_argv(config: SshConfig, remote_command: str) -> list[str]:
    return [
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
        remote_command,
    ]


def run_backup_checks(
    config: SshConfig | None,
    timeout: float = 20.0,
    *,
    now: datetime | None = None,
    capture=None,
) -> list[CheckResult]:
    """#895: are the weekly off-host backups and the restore rehearsal current?

    Each environment's latest manifest is judged by the SOP-004 verifier an
    operator runs by hand (every BackupFacet present, within its RPO, non-empty,
    checksummed, off-host), plus the manifest's own environment: a Staging run
    once overwrote the only pointer. The verifier is imported here, not at module
    top: other jobs import this module for alert delivery without PyYAML, and a
    broken import must stay a red check instead of a dead watchdog.
    """
    names = [name for name, _ in BACKUP_CHECKS] + [RESTORE_REHEARSAL_CHECK]
    if config is None:
        return [
            CheckResult(name, False, "SSH watchdog config is missing", "configuration")
            for name in names
        ]
    try:
        from libs.backup.verification import (
            FUTURE_TOLERANCE_HOURS,
            INVENTORY_DEFAULTS,
            latest_manifest_path,
            load_backup_inventory,
            verify_backup_manifest,
        )
        from tools.run_restore_rehearsal import ALL_SERVICES, PASS_SUMMARY_PREFIX

        inventory = load_backup_inventory()
    except Exception as exc:  # noqa: BLE001 - a broken verifier is red, never a pass.
        detail = (
            f"backup verifier unavailable: {type(exc).__name__}: {_one_line(str(exc))}"
        )
        return [CheckResult(name, False, detail, "configuration") for name in names]

    def run(remote_command: str) -> tuple[int, str, str]:
        if capture is not None:
            return capture(remote_command)
        completed = subprocess.run(
            _ssh_argv(config, remote_command),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return completed.returncode, completed.stdout, completed.stderr

    now_ts = int((now or datetime.now(UTC)).timestamp())
    results = []
    for name, environment in BACKUP_CHECKS:
        path = latest_manifest_path(environment)
        try:
            results.append(
                _backup_manifest_result(
                    name,
                    environment,
                    path,
                    run(f"cat {shlex.quote(path)}"),
                    inventory=inventory,
                    verify=verify_backup_manifest,
                    now_ts=now_ts,
                )
            )
        except Exception as exc:  # noqa: BLE001 - one bad input must not end main().
            results.append(
                CheckResult(
                    name,
                    False,
                    f"backup check raised {type(exc).__name__} on {path}: "
                    f"{_one_line(str(exc))}",
                    "backup",
                )
            )
    # The rehearsal runs 45 minutes after the weekly backup, so the backup RPO is
    # also the bound on how old the last proven restore may be.
    log = shlex.quote(RESTORE_REHEARSAL_LOG)
    crontab = shlex.quote(RESTORE_REHEARSAL_CRONTAB)
    try:
        completed = run(f"date -r {log} +%s && tail -n 5 {log}")
        schedule = None
        if completed[0] not in (0, SSH_TRANSPORT_FAILURE):
            schedule = run(f"date -r {crontab} +%s && cat {crontab}")
        results.append(
            _restore_rehearsal_result(
                completed,
                schedule,
                now_ts=now_ts,
                max_age_hours=INVENTORY_DEFAULTS["rpo_hours"],
                future_tolerance_hours=FUTURE_TOLERANCE_HOURS,
                expected_summary=PASS_SUMMARY_PREFIX + ", ".join(ALL_SERVICES),
            )
        )
    except Exception as exc:  # noqa: BLE001 - one bad input must not end main().
        results.append(
            CheckResult(
                RESTORE_REHEARSAL_CHECK,
                False,
                f"rehearsal check raised {type(exc).__name__} on "
                f"{RESTORE_REHEARSAL_LOG}: {_one_line(str(exc))}",
                "restore-rehearsal",
            )
        )
    return results


def _backup_manifest_result(
    name: str,
    environment: str,
    path: str,
    completed: tuple[int, str, str],
    *,
    inventory: list,
    verify,
    now_ts: int,
) -> CheckResult:
    returncode, stdout, stderr = completed
    if returncode == SSH_TRANSPORT_FAILURE:
        return CheckResult(
            name,
            False,
            f"could not reach the host to read {path}: {_last_line(stderr)}",
            "backup",
        )
    if returncode != 0:
        return CheckResult(
            name,
            False,
            f"no {environment} manifest at {path}: {_last_line(stderr or stdout)}",
            "backup",
        )
    try:
        manifest = json.loads(stdout)
    except ValueError as exc:
        return CheckResult(name, False, f"{path} is not JSON: {exc}", "backup")
    if not isinstance(manifest, dict):
        return CheckResult(name, False, f"{path} is not a JSON object", "backup")
    if manifest.get("environment") != environment:
        return CheckResult(
            name,
            False,
            f"{path} records environment {manifest.get('environment')!r}, "
            f"expected {environment!r}",
            "backup",
        )
    checks = verify(inventory, manifest, now=now_ts)["checks"]
    if not checks:
        # An empty inventory verifies nothing; 0 of 0 is not a pass.
        return CheckResult(name, False, "backup inventory is empty", "configuration")
    failed = [check for check in checks if check["status"] != "pass"]
    if failed:
        shown = "; ".join(
            f"{check['service_id']}: {check['summary']}" for check in failed[:6]
        )
        more = f" (+{len(failed) - 6} more)" if len(failed) > 6 else ""
        return CheckResult(
            name,
            False,
            f"{len(failed)}/{len(checks)} artifacts failed: {shown}{more}",
            "backup",
        )
    oldest = max(check["evidence"]["age_hours"] for check in checks)
    return CheckResult(
        name,
        True,
        f"{len(checks)}/{len(checks)} artifacts fresh and off-host; oldest {oldest}h",
        "backup",
    )


def _last_line(output: str) -> str:
    # ssh prepends "Warning: Permanently added <host ip> ..." to the command's stderr.
    lines = [line for line in output.strip().splitlines() if line.strip()]
    return _one_line(lines[-1]) if lines else ""


def _restore_rehearsal_result(
    completed: tuple[int, str, str],
    schedule: tuple[int, str, str] | None = None,
    *,
    now_ts: int,
    max_age_hours: int,
    future_tolerance_hours: int,
    expected_summary: str,
) -> CheckResult:
    returncode, stdout, stderr = completed
    if returncode == SSH_TRANSPORT_FAILURE:
        return CheckResult(
            RESTORE_REHEARSAL_CHECK,
            False,
            f"could not reach the host to read {RESTORE_REHEARSAL_LOG}: "
            f"{_last_line(stderr)}",
            "restore-rehearsal",
        )
    if returncode != 0:
        return _unrun_rehearsal_result(
            _last_line(stderr or stdout),
            schedule,
            now_ts=now_ts,
            max_age_hours=max_age_hours,
            future_tolerance_hours=future_tolerance_hours,
        )
    lines = stdout.splitlines()
    try:
        age_hours = (now_ts - int(lines[0].strip())) / 3600
    except (IndexError, ValueError):
        return CheckResult(
            RESTORE_REHEARSAL_CHECK,
            False,
            f"unreadable rehearsal log mtime: {_one_line(stdout)}",
            "restore-rehearsal",
        )
    last = next((line.strip() for line in reversed(lines[1:]) if line.strip()), "")
    if age_hours < -future_tolerance_hours:
        return CheckResult(
            RESTORE_REHEARSAL_CHECK,
            False,
            f"rehearsal log mtime is {-age_hours:.1f}h in the future (clock skew?)",
            "restore-rehearsal",
        )
    if age_hours > max_age_hours:
        return CheckResult(
            RESTORE_REHEARSAL_CHECK,
            False,
            f"last rehearsal wrote its log {age_hours:.1f}h ago (bound {max_age_hours}h)",
            "restore-rehearsal",
        )
    if last != expected_summary.strip():
        return CheckResult(
            RESTORE_REHEARSAL_CHECK,
            False,
            f"last rehearsal did not pass every service: {_one_line(last)}",
            "restore-rehearsal",
        )
    return CheckResult(
        RESTORE_REHEARSAL_CHECK,
        True,
        f"{last} ({age_hours:.1f}h ago)",
        "restore-rehearsal",
    )


def _unrun_rehearsal_result(
    missing: str,
    schedule: tuple[int, str, str] | None,
    *,
    now_ts: int,
    max_age_hours: int,
    future_tolerance_hours: int,
) -> CheckResult:
    """#926: no log yet, so the crontab decides between unscheduled, due and overdue.

    A cron installed mid-week has no log until its first slot, which is not a
    missed rehearsal. The clock is the crontab's mtime under the same bound as a
    stale log. Any crontab edit restarts it, but only until the first run writes
    the log; after that the log's own age is the bound.
    """

    def red(detail: str) -> CheckResult:
        return CheckResult(RESTORE_REHEARSAL_CHECK, False, detail, "restore-rehearsal")

    no_log = f"no rehearsal log at {RESTORE_REHEARSAL_LOG} ({missing})"
    if schedule is None:
        return red(f"{no_log}, so the weekly restore rehearsal has never run")
    returncode, stdout, stderr = schedule
    if returncode == SSH_TRANSPORT_FAILURE:
        return red(
            f"{no_log}, and could not reach the host to read "
            f"{RESTORE_REHEARSAL_CRONTAB}: {_last_line(stderr)}"
        )
    if returncode != 0:
        return red(
            f"{no_log}, and {RESTORE_REHEARSAL_CRONTAB} is unreadable: "
            f"{_last_line(stderr or stdout)}"
        )
    lines = stdout.splitlines()
    try:
        age_hours = (now_ts - int(lines[0].strip())) / 3600
    except (IndexError, ValueError):
        return red(f"{no_log}, and unreadable {RESTORE_REHEARSAL_CRONTAB} mtime")
    # Never echo the crontab itself: the detail lands in a public GitHub issue.
    if not any(
        RESTORE_REHEARSAL_LOG in line and not line.lstrip().startswith("#")
        for line in lines[1:]
    ):
        return red(
            f"{no_log}, and no entry in {RESTORE_REHEARSAL_CRONTAB} writes it, "
            "so the weekly restore rehearsal is not scheduled"
        )
    if age_hours < -future_tolerance_hours:
        return red(
            f"{no_log}, and {RESTORE_REHEARSAL_CRONTAB} mtime is "
            f"{-age_hours:.1f}h in the future (clock skew?)"
        )
    if age_hours > max_age_hours:
        return red(
            f"{no_log}, though {RESTORE_REHEARSAL_CRONTAB} was last written "
            f"{age_hours:.1f}h ago (bound {max_age_hours}h), so the weekly restore "
            "rehearsal has never run"
        )
    return CheckResult(
        RESTORE_REHEARSAL_CHECK,
        True,
        f"first rehearsal not yet due: no log yet, and {RESTORE_REHEARSAL_CRONTAB} "
        f"was last written {age_hours:.1f}h ago (bound {max_age_hours}h)",
        "restore-rehearsal",
    )


def run_dokploy_status_check(
    env: Mapping[str, str],
    *,
    client_factory=get_dokploy,
    unit_runtime=None,
) -> list[CheckResult]:
    """Read Dokploy's per-compose/app status for the daily report (#908).

    A missing or rejected DOKPLOY_API_KEY is a configuration failure, and a
    configuration failure pages. Any compose/application whose status is exactly
    ``error`` (case-insensitive) is a report-only failure; ``idle``/``done``/
    ``running`` (and anything else) are not findings. For a compose in ``error``,
    `unit_runtime(project) -> (returncode, stdout, stderr)` runs
    unit_runtime_command on the host; when that unit's own containers run healthy
    on the current config the record is stale (split_stale_dokploy_records).
    """
    if not env.get("DOKPLOY_API_KEY", "").strip():
        return [
            CheckResult(
                DOKPLOY_STATUS_CHECK,
                False,
                "DOKPLOY_API_KEY is missing",
                "configuration",
            )
        ]

    host = env.get("DOKPLOY_STATUS_HOST", "").strip() or "cloud.zitian.party"
    try:
        client = client_factory(host=host)
        projects = client.list_projects()
        results: list[CheckResult] = []
        for project in projects or []:
            project_name = project.get("name", "unknown")
            for environment in project.get("environments", []) or []:
                env_name = environment.get("name", "unknown")
                for compose in environment.get("compose", []) or []:
                    evidence = (
                        _dokploy_compose_error_evidence(client, compose, unit_runtime)
                        if _dokploy_status_is_error(compose.get("composeStatus"))
                        else {}
                    )
                    results.extend(
                        _dokploy_status_failure(
                            project_name,
                            env_name,
                            compose.get("name", "unknown"),
                            compose.get("composeStatus"),
                            "composeStatus",
                            evidence,
                        )
                    )
                for application in environment.get("applications", []) or []:
                    results.extend(
                        _dokploy_status_failure(
                            project_name,
                            env_name,
                            application.get("name", "unknown"),
                            application.get("applicationStatus"),
                            "applicationStatus",
                            {},
                        )
                    )
    except Exception as exc:  # noqa: BLE001 - watchdog must turn errors into alerts.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (401, 403):
            # The key is wrong or revoked: the watchdog is blind to Dokploy.
            return [
                CheckResult(
                    DOKPLOY_STATUS_CHECK,
                    False,
                    f"Dokploy rejected DOKPLOY_API_KEY: HTTP {status}",
                    "configuration",
                )
            ]
        return [
            CheckResult(
                DOKPLOY_STATUS_CHECK,
                False,
                f"dokploy status query raised {type(exc).__name__}: "
                f"{_one_line(str(exc))}",
                "dokploy-control-plane",
            )
        ]
    return results


def _dokploy_status_failure(
    project_name: str,
    env_name: str,
    unit_name: str,
    status: object,
    status_field: str,
    evidence: Mapping[str, object],
) -> list[CheckResult]:
    if not _dokploy_status_is_error(status):
        return []
    detail_parts = [f"{status_field}=error"]
    for key in (
        "composeId",
        "latest_deployment_id",
        "latest_deployment_status",
        "latest_deployment_logPath",
        "latest_deployment_errorMessage",
        "latest_deployment_error",
    ):
        value = evidence.get(key)
        if value:
            detail_parts.append(f"{key}={_one_line(str(value))}")
    proven = evidence.get("runtime_proven") is True
    runtime = str(evidence.get("runtime") or "")
    if runtime and not proven:
        detail_parts.append(f"runtime: {runtime}")
    recorded_at = evidence.get("latest_deployment_at")
    return [
        CheckResult(
            f"{DOKPLOY_STATUS_FAMILY}{project_name}/{env_name}/{unit_name}",
            False,
            " ".join(detail_parts),
            "dokploy-deploy-status",
            recorded_at=recorded_at if isinstance(recorded_at, float) else None,
            runtime_evidence=runtime if proven else "",
        )
    ]


def _dokploy_status_is_error(status: object) -> bool:
    return isinstance(status, str) and status.strip().lower() == "error"


def _dokploy_compose_error_evidence(
    client: object, compose: Mapping[str, object], unit_runtime=None
) -> dict[str, object]:
    compose_id = str(compose.get("composeId") or compose.get("id") or "").strip()
    if not compose_id:
        return {}
    evidence: dict[str, object] = {"composeId": compose_id}
    proven, why = _dokploy_unit_runtime(client, compose_id, unit_runtime)
    evidence.update(runtime_proven=proven, runtime=why)
    get_latest_deployment = getattr(client, "get_latest_deployment", None)
    if not callable(get_latest_deployment):
        return evidence
    try:
        latest = get_latest_deployment(compose_id)
    except Exception as exc:  # noqa: BLE001 - evidence must not mask status alerts.
        evidence["latest_deployment_error"] = (
            f"{type(exc).__name__}: {_one_line(str(exc))}"
        )
        return evidence
    if isinstance(latest, Mapping):
        evidence.update(_dokploy_deployment_evidence(latest))
        recorded_at = deployment_start_epoch(dict(latest))
        if recorded_at is not None:
            evidence["latest_deployment_at"] = float(recorded_at)
    return evidence


@dataclass(frozen=True)
class UnitContainer:
    name: str
    state: str
    exit_code: str
    health: str
    config_hash: str


def unit_runtime_command(project: str) -> str:
    """List one Dokploy compose unit's containers (compose project = appName)."""
    label = shlex.quote(f"label=com.docker.compose.project={project}")
    return (
        f"ids=$(docker ps -aq --filter {label}) && "
        f'if [ -n "$ids" ]; then docker inspect --format '
        f"{shlex.quote(UNIT_RUNTIME_FORMAT)} $ids; fi"
    )


def parse_unit_containers(output: str) -> list[UnitContainer]:
    """Containers from unit_runtime_command. A line it cannot read is an error:
    skipping it could drop the one unhealthy container and prove the unit well."""
    containers = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.strip().split("|")
        if len(parts) != 5 or not parts[0].strip("/"):
            raise ValueError(f"unreadable container line: {_one_line(line)[:120]}")
        name, state, exit_code, health, config_hash = (part.strip() for part in parts)
        containers.append(
            UnitContainer(name.lstrip("/"), state, exit_code, health, config_hash)
        )
    return containers


def unit_runtime_verdict(
    containers: list[UnitContainer], expected_hash: str
) -> tuple[bool, str]:
    """Does this unit run healthy on the current config? (proven, why)

    Every container must be running and healthy (or have no health check), or be
    a one-shot that exited 0; every container that carries IAC_CONFIG_HASH must
    carry the current one, and at least one must carry it. An old version still
    running healthy after a failed deploy is exactly what this rejects.
    """
    if not expected_hash:
        return False, (
            f"Dokploy holds no {CONFIG_HASH_KEY} for this unit, so its current "
            "config cannot be matched"
        )
    if not containers:
        return False, "no container of this unit on the host"
    problems = []
    for container in containers:
        running_well = container.state == "running" and container.health in {
            "healthy",
            "none",
        }
        finished_one_shot = container.state == "exited" and container.exit_code == "0"
        if not (running_well or finished_one_shot):
            problems.append(
                f"{container.name} is {container.state} (health {container.health}, "
                f"exit {container.exit_code})"
            )
    hashed = [container for container in containers if container.config_hash]
    for container in hashed:
        if container.config_hash != expected_hash:
            problems.append(
                f"{container.name} runs config {container.config_hash[:12]}, "
                f"current is {expected_hash[:12]}"
            )
    if not hashed:
        problems.append(f"no container carries {CONFIG_HASH_KEY}")
    if not any(container.state == "running" for container in containers):
        problems.append("no container is running")
    if problems:
        return False, "; ".join(problems)
    return True, (
        f"{len(containers)} container(s) of this unit run healthy on the current "
        f"config {expected_hash[:12]}"
    )


def _dokploy_unit_runtime(
    client: object, compose_id: str, unit_runtime
) -> tuple[bool, str]:
    """Per-unit runtime evidence for one compose in `error`; never raises."""
    if unit_runtime is None:
        return False, "no host access for per-unit runtime evidence"
    get_compose = getattr(client, "get_compose", None)
    if not callable(get_compose):
        return False, "the Dokploy client cannot read the compose"
    try:
        record = get_compose(compose_id)
        project = str(record.get("appName") or "").strip()
        expected = ""
        for line in str(record.get("env") or "").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == CONFIG_HASH_KEY:
                expected = value.strip()
        if not project:
            return (
                False,
                "Dokploy reports no appName, so its containers cannot be found",
            )
        returncode, stdout, stderr = unit_runtime(project)
        if returncode != 0:
            return False, (
                f"could not list the unit's containers (exit {returncode}): "
                f"{_last_line(stderr or stdout)}"
            )
        return unit_runtime_verdict(parse_unit_containers(stdout), expected)
    except Exception as exc:  # noqa: BLE001 - no evidence is not a crash.
        return False, (
            f"runtime evidence unavailable: {type(exc).__name__}: "
            f"{_one_line(str(exc))[:160]}"
        )


def _dokploy_deployment_evidence(deployment: Mapping[str, object]) -> dict[str, object]:
    mapping = {
        "id": deployment.get("deploymentId") or deployment.get("id"),
        "status": deployment.get("status"),
        "logPath": deployment.get("logPath"),
        "errorMessage": _one_line(str(deployment.get("errorMessage") or "")),
    }
    return {
        f"latest_deployment_{key}": value for key, value in mapping.items() if value
    }


def run_peer_scheduler_liveness_check(
    env: Mapping[str, str],
    timeout: float = 10.0,
    *,
    max_attempts: int = 2,
    retry_delay_seconds: float = 60.0,
    getter_factory=github_getter,
    now: datetime | None = None,
) -> list[CheckResult]:
    """truealpha#876: is truealpha's scheduler-liveness workflow still ticking?

    That workflow watches every scheduled workflow in the estate but cannot
    report its own dead scheduler; this is the peer. An unreadable answer is
    red (retried once, like the HTTP checks); the verdict itself is not retried.
    """
    try:
        bound_cap = parse_bound_cap_hours(env.get(BOUND_CAP_ENV))
    except ValueError as exc:
        return [
            CheckResult(
                PEER_SCHEDULER_LIVENESS_CHECK,
                False,
                f"{BOUND_CAP_ENV}: {exc}",
                "configuration",
            )
        ]
    getter = getter_factory(env.get("GITHUB_TOKEN", "").strip(), timeout=timeout)
    attempts = max(1, max_attempts)
    detail = ""
    status = UNVERIFIABLE
    for attempt in range(1, attempts + 1):
        try:
            verdict = evaluate_peer_liveness(
                getter, now=now or datetime.now(UTC), bound_cap=bound_cap
            )
            status, detail = verdict.status, verdict.detail
        except Exception as exc:  # noqa: BLE001 - an unreadable peer is red, never a pass.
            status = UNVERIFIABLE
            detail = f"peer check raised {type(exc).__name__}: {_one_line(str(exc))}"
        if status != UNVERIFIABLE or attempt == attempts:
            break
        if retry_delay_seconds > 0:
            time.sleep(retry_delay_seconds)
    ok = status == PEER_OK
    return [
        CheckResult(
            PEER_SCHEDULER_LIVENESS_CHECK,
            ok,
            f"{status}: {detail}",
            "" if ok else "peer-scheduler-liveness",
            attempt_count=attempt,
        )
    ]


def split_stale_dokploy_records(
    results: list[CheckResult], *, now_ts: float
) -> tuple[list[CheckResult], list[tuple[CheckResult, str]]]:
    """#908: separate stale Dokploy `error` records from current failures.

    A record is stale only with per-unit runtime evidence: that unit's own
    containers run healthy on the current config (unit_runtime_verdict). A
    host-wide sweep cannot see a failed pull that left the old version running,
    and neither can age. A record older than DOKPLOY_RECORD_MAX_AGE_HOURS without
    that evidence stays a failure, labelled with its age. Stale records are listed
    in the daily report for reconciliation (ops.standards.md Rule 4), not dropped.
    """
    bound_seconds = DOKPLOY_RECORD_MAX_AGE_HOURS * 3600
    kept: list[CheckResult] = []
    stale: list[tuple[CheckResult, str]] = []
    for result in results:
        if result.ok or not result.name.startswith(DOKPLOY_STATUS_FAMILY):
            kept.append(result)
        elif result.runtime_evidence:
            stale.append((result, result.runtime_evidence))
        elif (
            result.recorded_at is not None
            and now_ts - result.recorded_at > bound_seconds
        ):
            age_hours = (now_ts - result.recorded_at) / 3600
            kept.append(
                replace(
                    result,
                    detail=(
                        f"{result.detail}; old record: latest deployment is "
                        f"{age_hours:.0f}h old (over {DOKPLOY_RECORD_MAX_AGE_HOURS}h), "
                        "and no per-unit runtime evidence shows the unit recovered"
                    ),
                )
            )
        else:
            kept.append(result)
    return kept, stale


def _severity_for(name: str, failure_domain: str) -> str:
    """Map a failure to a P0/P1/P2 severity ladder.

    P0 — host/alerting itself at risk; P1 — prod or control-plane degraded;
    P2 — non-prod / single-container diagnostics.
    """
    if failure_domain in {
        "host-reachability",
        "docker-runtime",
        "alert-bridge",
        "configuration",
    }:
        return "P0"
    if failure_domain in {
        "cloudflare-worker-health",
        "dokploy-control-plane",
        # a dead peer scheduler hides every other scheduled check's staleness
        "peer-scheduler-liveness",
        # report-only findings reached no one
        "report-delivery",
    }:
        return "P1"
    if failure_domain in {"backup", "restore-rehearsal"}:
        # Production data is unprotected until the next weekly run; Staging is not.
        return "P2" if "staging" in name.lower() else "P1"
    if failure_domain in {"dokploy-deploy-status", "public-route"}:
        lowered = name.lower()
        if "staging" in lowered or "-pr-" in lowered or "preview" in lowered:
            return "P2"
        return "P1"
    return "P2"


#: 影响 per failure domain (#905), next to the action and runbook maps below.
_IMPACT_BY_DOMAIN = {
    "host-reachability": "VPS 或它的公网入口不可达:所有产品可能不可用",
    "docker-runtime": "容器运行时异常:其上的服务降级或不可用",
    "alert-bridge": "带内告警出口(bridge)不可用:VPS 上的故障无法送达",
    "configuration": "watchdog 自身配置缺失或被拒:它看不见的故障没人报",
    "cloudflare-worker-health": "Cloudflare 带外 watchdog 停跑或投递失败:整机失联时无人报警",
    "dokploy-control-plane": "Dokploy 控制面异常:部署与回滚受阻",
    "peer-scheduler-liveness": "truealpha 的 scheduler-liveness 死了:定时 workflow 停跑无人报",
    "report-delivery": "报告投递失败:只报告的发现没有送到任何人",
    "backup": "异地备份缺失或过期:数据在下次成功备份前无保护",
    "restore-rehearsal": "恢复演练未通过或未运行:备份能否恢复没有证据",
    "dokploy-deploy-status": "Dokploy 单元处于 error:该服务可能停在旧版本或不可用",
    "public-route": "公网路由不可达:用户打不开该服务",
}
_DEFAULT_IMPACT = "未归类的检查失败:按现象判断影响范围"


def format_failure_message(
    results: list[CheckResult], *, run_url: str, now: float | None = None
) -> str:
    """Build the Feishu page for failed paging checks (see route_failures).

    #905: the pager layout every source shares (``libs.alerting.PAGER_FIELDS``).
    """
    now = time.time() if now is None else now
    failures = sorted(
        (result for result in results if not result.ok),
        key=lambda result: pager_level(result.severity),
    )
    level = highest_level(result.severity for result in failures)
    items = tuple(
        PagerItem(
            level=pager_level(result.severity),
            environment=_check_environment(result),
            target=result.name,
            symptom=_redact(result.detail),
            since=f"{format_utc(now)} 检出(日级审计;实际开始时间未知)",
            impact=(f"[{result.failure_domain}] " if result.failure_domain else "")
            + _IMPACT_BY_DOMAIN.get(result.failure_domain, _DEFAULT_IMPACT),
            action=_suggested_action_for_failure(result.name, result.failure_domain),
            runbook=_runbook_url_for_failure(result.failure_domain),
        )
        for result in failures
    )
    preamble = ["来源：GitHub Actions 日级带外审计 → 飞书直发(不经 bridge)"]
    if run_url:
        preamble.append(f"运行：{run_url}")
    return render_pager_text(
        PagerMessage(
            title=firing_title(level, f"GitHub 日级带外审计 · {len(items)} 项"),
            firing=items,
            preamble=tuple(preamble),
        )
    )


def _check_environment(result: CheckResult) -> str:
    """环境 of a GitHub check: staging by name, a Dokploy unit's own, production for
    the production data checks, else global (the host and the watchdogs)."""
    name = result.name.lower()
    if "staging" in name:
        return "staging"
    if name.startswith(DOKPLOY_STATUS_FAMILY):
        parts = name.removeprefix(DOKPLOY_STATUS_FAMILY).split("/")
        if len(parts) >= 2 and parts[1]:
            return parts[1]
    if result.failure_domain in {"backup", "restore-rehearsal"}:
        return "production"
    return "global"


def load_report_only_checks(path: Path = SIGNAL_REGISTRY) -> frozenset[str]:
    """The GitHub checks that only report: registered `primary_owner: github` with
    `type: report`. Every other check pages, including a name the registry never
    saw (a target added through `vars.INFRA2_WATCHDOG_*_TARGETS`).

    Imported lazily: other jobs import this module for alert delivery without
    PyYAML. A registry whose GitHub signals are all reports would leave the
    GitHub layer's own classes without a pager, so that is an error.
    """
    import yaml

    inventory = yaml.safe_load(path.read_text(encoding="utf-8"))
    github = [
        signal
        for signal in inventory["signals"]
        if signal.get("primary_owner") == "github"
    ]
    report_only = frozenset(
        str(signal["signal"]) for signal in github if signal.get("type") == "report"
    )
    if len(report_only) == len(github):
        raise ValueError(f"{path.name} registers no paging GitHub signal")
    return report_only


def registry_signal(name: str) -> str:
    """The registered signal a check reports under (a Dokploy unit -> its family)."""
    return DOKPLOY_STATUS_CHECK if name.startswith(DOKPLOY_STATUS_FAMILY) else name


def pages(result: CheckResult, report_only: frozenset[str] | None) -> bool:
    """Does this failure page? `report_only=None` (registry unreadable) pages all."""
    return (
        report_only is None
        or registry_signal(result.name) not in report_only
        or result.failure_domain in WATCHDOG_SELF_DOMAINS
    )


def route_failures(
    results: list[CheckResult], report_only: frozenset[str] | None
) -> tuple[list[CheckResult], list[CheckResult]]:
    """Split failures into (paged, reported) -- #908, ops.observability.md §1.1.

    A check pages unless the registry marks it `type: report`, i.e. another layer
    pages its failure class; failures of the watchdog itself always page.
    """
    failures = [result for result in results if not result.ok]
    paged = [result for result in failures if pages(result, report_only)]
    reported = [result for result in failures if not pages(result, report_only)]
    return paged, reported


def format_report_message(
    reported: list[CheckResult],
    stale: list[tuple[CheckResult, str]],
    notes: list[str],
    *,
    run_url: str,
) -> str:
    """The daily report for what this watchdog reports but does not page.

    Empty when there is nothing to say: the other daily reports already prove the
    reports chat delivers, so a green day sends nothing from this job.
    """
    if not (reported or stale or notes):
        return ""
    lines = [
        f"{REPORT_TITLE_PREFIX}Infra2 GitHub watchdog 日级审计",
        "Reported, not paged: another layer pages these failure classes "
        "(ops.observability.md §1.1).",
    ]
    if run_url:
        lines.append(f"Run: {run_url}")
    if reported:
        lines.append(f"Failures ({len(reported)}):")
        for result in reported:
            domain = f"[{result.failure_domain}] " if result.failure_domain else ""
            lines.append(
                f"- [{result.severity}] {domain}{result.name}: {_redact(result.detail)}"
            )
    if stale:
        lines.append(
            f"Stale Dokploy records ({len(stale)}), not failures; reconcile per "
            f"{STATE_DISCREPANCY_RUNBOOK}:"
        )
        for result, reason in stale:
            lines.append(f"- {result.name}: {_redact(result.detail)} [{reason}]")
    if notes:
        lines.append("Information:")
        lines.extend(f"- {_redact(note)}" for note in notes)
    return "\n".join(lines)


def _undelivered(reported: list[CheckResult], stale_count: int, note_count: int) -> str:
    """What a report that cannot be sent would have said, for the page instead."""
    parts = []
    if reported:
        names = ", ".join(result.name for result in reported)
        parts.append(f"{len(reported)} report-only failure(s) ({names})")
    if stale_count:
        parts.append(f"{stale_count} stale Dokploy record(s)")
    if note_count:
        parts.append(f"{note_count} information line(s)")
    return f"; undelivered: {', '.join(parts)}" if parts else ""


def _deliver_report(
    env: Mapping[str, str],
    report: str,
    reported: list[CheckResult],
    *,
    stale_count: int = 0,
    note_count: int = 0,
    dry_run: bool,
) -> CheckResult | None:
    """Send the report; a report path that is missing or broken pages instead."""
    undelivered = _undelivered(reported, stale_count, note_count) if report else ""
    missing = [name for name in INFRA2_REPORTS_ENV if not env.get(name, "").strip()]
    if missing:
        return CheckResult(
            REPORT_CHECK,
            False,
            f"report path is not configured: {', '.join(missing)} missing{undelivered}",
            "configuration",
        )
    if not report:
        return None
    if dry_run:
        print(report)
        return None
    try:
        deliver_infra2_report(report, env)
    except Exception as exc:  # noqa: BLE001 - a lost report must page, not vanish.
        _emit_structured_log(
            {
                "event": "watchdog.report.delivery.failure",
                "status": "fail",
                "error": _redact(_one_line(str(exc))),
            }
        )
        return CheckResult(
            REPORT_CHECK,
            False,
            f"report delivery failed: {_one_line(str(exc))}{undelivered}",
            "report-delivery",
        )
    _emit_structured_log(
        {
            "event": "watchdog.report.delivery.success",
            "status": "ok",
            "report_failure_count": len(reported),
            "route": "github-actions->infra2-reports",
        }
    )
    return None


def main(env: Mapping[str, str] | None = None) -> int:
    """Run watchdog checks; page the owned classes, report the rest (#908)."""
    current_env = env or os.environ
    http_targets = parse_http_targets(
        current_env.get("INFRA2_WATCHDOG_HTTP_TARGETS", "")
    )
    ssh_targets = parse_ssh_targets(current_env.get("INFRA2_WATCHDOG_SSH_TARGETS", ""))
    timeout = _env_float(current_env, "INFRA2_WATCHDOG_HTTP_TIMEOUT", 10.0)
    retry_max_attempts = _env_int(current_env, "INFRA2_WATCHDOG_RETRY_MAX_ATTEMPTS", 2)
    retry_delay_seconds = _env_float(
        current_env, "INFRA2_WATCHDOG_RETRY_DELAY_SECONDS", 60.0
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
    results.extend(
        run_dokploy_status_check(current_env, unit_runtime=_ssh_capture(ssh_config))
    )
    results.extend(run_ssh_checks(ssh_config, ssh_targets))
    results.extend(run_backup_checks(ssh_config))
    results.extend(
        run_peer_scheduler_liveness_check(
            current_env,
            timeout,
            max_attempts=retry_max_attempts,
            retry_delay_seconds=retry_delay_seconds,
        )
    )
    results = [
        replace(result, severity=_severity_for(result.name, result.failure_domain))
        for result in results
    ]
    results, stale = split_stale_dokploy_records(results, now_ts=time.time())
    try:
        report_only: frozenset[str] | None = load_report_only_checks()
    except Exception as exc:  # noqa: BLE001 - unknown routing pages everything.
        report_only = None
        results.append(
            CheckResult(
                SIGNAL_REGISTRY_CHECK,
                False,
                f"cannot read {SIGNAL_REGISTRY.name}, so every failure pages: "
                f"{type(exc).__name__}: {_one_line(str(exc))}",
                "configuration",
                severity=_severity_for(SIGNAL_REGISTRY_CHECK, "configuration"),
            )
        )
    paged, reported = route_failures(results, report_only)
    dry_run = current_env.get("WATCHDOG_DRY_RUN") == "1"
    notes = [result.note for result in results if result.note]
    report = format_report_message(
        reported, stale, notes, run_url=_github_run_url(current_env)
    )
    report_failure = _deliver_report(
        current_env,
        report,
        reported,
        stale_count=len(stale),
        note_count=len(notes),
        dry_run=dry_run,
    )
    if report_failure is not None:
        report_failure = replace(
            report_failure,
            severity=_severity_for(report_failure.name, report_failure.failure_domain),
        )
        results.append(report_failure)
        paged.append(report_failure)

    _record_issue_trail_verdicts(current_env, results, report_only)
    for result in results:
        route = "" if result.ok else PAGE if pages(result, report_only) else REPORT
        _emit_structured_log(
            {
                "event": "watchdog.check",
                "name": result.name,
                "status": "ok" if result.ok else "fail",
                "route": route,
                "failure_domain": result.failure_domain,
                "attempt_count": result.attempt_count,
                "detail": _redact(result.detail),
            }
        )
        suffix = " [reported, not paged]" if route == REPORT else ""
        status = "OK" if result.ok else "FAIL"
        print(f"{status} {result.name}: {_redact(result.detail)}{suffix}")
    for result, reason in stale:
        _emit_structured_log(
            {
                "event": "watchdog.check",
                "name": result.name,
                "status": "stale",
                "route": REPORT,
                "failure_domain": result.failure_domain,
                "attempt_count": result.attempt_count,
                "detail": _redact(f"{result.detail}; {reason}"),
            }
        )
        print(f"STALE {result.name}: {_redact(result.detail)} [{reason}]")

    if not paged:
        _emit_structured_log(
            {
                "event": "watchdog.run.complete",
                "status": "ok",
                "failure_count": 0,
                "report_failure_count": len(reported),
            }
        )
        return 0

    message = format_failure_message(paged, run_url=_github_run_url(current_env))
    if dry_run:
        print(message)
        _emit_structured_log(
            {
                "event": "watchdog.run.complete",
                "status": "fail",
                "failure_count": len(paged),
                "report_failure_count": len(reported),
                "dry_run": True,
            }
        )
        return 1

    try:
        deliver_out_of_band_alert(current_env, message)
    except Exception as exc:  # noqa: BLE001 - watchdog must not fail silently.
        fallback_issue_url = create_delivery_fallback_issue(
            current_env, failures=paged, error=str(exc)
        )
        _emit_structured_log(
            {
                "event": "watchdog.delivery.failure",
                "status": "fail",
                "failure_count": len(paged),
                "error": _redact(_one_line(str(exc))),
                "fallback_issue_url": fallback_issue_url or "",
            }
        )
        return 1
    _emit_structured_log(
        {
            "event": "watchdog.delivery.success",
            "status": "ok",
            "failure_count": len(paged),
            "route": "github-actions->feishu-direct",
        }
    )
    _emit_structured_log(
        {
            "event": "watchdog.run.complete",
            "status": "fail",
            "failure_count": len(paged),
            "report_failure_count": len(reported),
            "dry_run": False,
        }
    )
    return 1


def _record_issue_trail_verdicts(
    env: Mapping[str, str],
    results: list[CheckResult],
    report_only: frozenset[str] | None,
) -> None:
    """Hand this run's PAGING verdicts to the issue-trail step (truealpha#876 W4).

    Report-only checks never open issues (#908): their failures are report lines.
    A failure of the watchdog itself that surfaced on a report-only check (its
    configuration, the report path) is recorded as the watchdog step's own red
    verdict, which the next run that records cleanly closes. When the registry is
    unreadable nothing but that verdict is recorded, and the record says it is
    incomplete so the trail does not close issues of checks it did not record.
    """
    path = env.get(VERDICTS_ENV, "").strip()
    if not path:
        return

    def is_paging(result: CheckResult) -> bool:
        return (
            report_only is not None
            and result.name not in WATCHDOG_SELF_CHECKS
            and registry_signal(result.name) not in report_only
        )

    checks = [
        CheckVerdict(
            result.name,
            result.ok,
            _redact(_one_line(result.detail)),
            result.severity,
            result.failure_domain,
        )
        for result in results
        if is_paging(result)
    ]
    own = [
        result
        for result in results
        if not result.ok
        and result.failure_domain in WATCHDOG_SELF_DOMAINS
        and not is_paging(result)
    ]
    if own:
        checks.append(
            CheckVerdict(
                WATCHDOG_SOURCE,
                False,
                _redact(
                    _one_line(
                        "; ".join(f"{result.name}: {result.detail}" for result in own)
                    )
                ),
                highest_level(result.severity for result in own),
                "configuration",
            )
        )
    try:
        record_verdicts(
            path,
            source=WATCHDOG_SOURCE,
            checks=checks,
            complete=report_only is not None,
        )
    except OSError as exc:
        # The issue-trail step then finds no verdict from this source: red.
        print(f"could not record verdicts for the issue trail: {exc}", file=sys.stderr)


def _ssh_capture(config: SshConfig | None, timeout: float = 20.0):
    """`command -> (returncode, stdout, stderr)` on the host, or None without SSH."""
    if config is None:
        return None

    def capture(remote_command: str) -> tuple[int, str, str]:
        completed = subprocess.run(
            _ssh_argv(config, remote_command),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return completed.returncode, completed.stdout, completed.stderr

    def unit_runtime(project: str) -> tuple[int, str, str]:
        return capture(unit_runtime_command(project))

    return unit_runtime


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
    if failure_domain == "dokploy-control-plane":
        return "check Dokploy API and deploy logs for rollout/network failures"
    if failure_domain == "peer-scheduler-liveness":
        return (
            "open truealpha's scheduler-liveness workflow: re-enable it if disabled, "
            "check its newest scheduled run and githubstatus.com; while it is dead no "
            "stopped scheduled workflow in the estate is reported"
        )
    if failure_domain == "backup":
        return (
            "on the host, read /var/log/infra2-backup*.log for FAILED lines, rerun "
            "/usr/local/sbin/infra2-host-backup.sh per SOP-006, then verify the "
            "manifest per SOP-004"
        )
    if failure_domain == "restore-rehearsal":
        return (
            "on the host, read /var/log/infra2-backup-restore-rehearsal.log (or, if "
            "it is missing, root's crontab) and rerun or install the rehearsal cron "
            "command per SOP-006A"
        )
    if failure_domain == "report-delivery":
        return (
            "check the INFRA2_REPORTS_FEISHU_* secrets and the reports app; the "
            "undelivered report-only findings are in this run's log"
        )
    return "inspect the failed check detail and verify target service health manually"


def _runbook_url_for_failure(failure_domain: str) -> str:
    if failure_domain in {"host-reachability", "docker-runtime"}:
        anchor = (
            "#watchdog-silent"
            if failure_domain == "host-reachability"
            else "#container-killed"
        )
        return (
            "https://github.com/wangzitian0/infra2/blob/main/"
            f"docs/runbooks/infra022-p0.md{anchor}"
        )
    if failure_domain in {"backup", "restore-rehearsal"}:
        anchor = (
            "#sop-004-备份-freshness-验证"
            if failure_domain == "backup"
            else "#sop-006a-off-host-restore-rehearsal"
        )
        return (
            "https://github.com/wangzitian0/infra2/blob/main/"
            f"docs/ssot/ops.recovery.md{anchor}"
        )
    if failure_domain == "peer-scheduler-liveness":
        return (
            "https://github.com/wangzitian0/truealpha/actions/workflows/"
            "scheduler-liveness.yml"
        )
    anchor = "#out-of-band-watchdog"
    if failure_domain == "alert-bridge":
        anchor = "#alerting-bridge"
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


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None:
        return default
    value = str(raw).strip()
    if not value:
        return default
    return int(value)


def _env_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None:
        return default
    value = str(raw).strip()
    if not value:
        return default
    return float(value)


def _emit_structured_log(payload: Mapping[str, object]) -> None:
    body = dict(payload)
    body.setdefault("timestamp", int(time.time()))
    print(json.dumps(body, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
