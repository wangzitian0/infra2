"""Infra service probe helpers for code-owned alert checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
import shlex
import socket
import subprocess
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_TIMEOUT_SECONDS = 5.0
HTTP_PROBE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; infra2-probe/1.0; +https://zitian.party)",
    "Accept": "text/html,application/json,text/plain,*/*",
}
PROBE_CLIENT_BLOCKED_MARKERS = ("error code: 1010",)


@dataclass(frozen=True)
class ProbeSpec:
    name: str
    kind: str
    target: str
    expected: str = ""
    severity: str = "critical"
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    # Optional name of another probe this one depends on. If BOTH fail in the same cycle,
    # this probe's failure is a cascade symptom of that root, so its alert is suppressed
    # (page the root, not the dependents). Empty = independent.
    depends_on: str = ""


@dataclass(frozen=True)
class ProbeResult:
    spec: ProbeSpec
    ok: bool
    summary: str
    observed: str
    elapsed_ms: int

    def to_dict(self) -> dict:
        data = asdict(self)
        data["spec"] = asdict(self.spec)
        return data


def parse_probe_specs(raw: str) -> list[ProbeSpec]:
    """Parse newline-separated probe specs.

    Format: name|kind|target|expected|severity|timeout_seconds|depends_on
    """
    specs: list[ProbeSpec] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 3:
            raise ValueError(f"Invalid probe spec: {line}")
        timeout = (
            float(parts[5]) if len(parts) > 5 and parts[5] else DEFAULT_TIMEOUT_SECONDS
        )
        specs.append(
            ProbeSpec(
                name=parts[0],
                kind=parts[1],
                target=parts[2],
                expected=parts[3] if len(parts) > 3 else "",
                severity=parts[4] if len(parts) > 4 and parts[4] else "critical",
                timeout_seconds=timeout,
                depends_on=parts[6] if len(parts) > 6 and parts[6] else "",
            )
        )
    return specs


def run_probe(
    spec: ProbeSpec,
    *,
    http_get: Callable[[str, float], tuple[int, str]] | None = None,
    tcp_connect: Callable[[str, int, float], None] | None = None,
    command_runner: Callable[[str, float], subprocess.CompletedProcess[str]]
    | None = None,
) -> ProbeResult:
    started = time.monotonic()
    try:
        if spec.kind == "http":
            observed = _run_http(spec, http_get=http_get)
        elif spec.kind == "tcp":
            observed = _run_tcp(spec, tcp_connect=tcp_connect)
        elif spec.kind == "command":
            observed = _run_command(spec, command_runner=command_runner)
        elif spec.kind == "resource":
            observed = _run_resource(spec)
        else:
            raise ValueError(f"Unsupported probe kind: {spec.kind}")
        ok = _matches_expected(spec, observed)
        summary = (
            "probe passed"
            if ok
            else f"expected {spec.expected!r}, observed {observed!r}"
        )
    except Exception as exc:  # noqa: BLE001 - probes must classify all failures.
        observed = exc.__class__.__name__
        ok = False
        summary = str(exc) or observed
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return ProbeResult(
        spec=spec, ok=ok, summary=summary, observed=observed, elapsed_ms=elapsed_ms
    )


def run_probes(specs: list[ProbeSpec]) -> list[ProbeResult]:
    return [run_probe(spec) for spec in specs]


def failed_results(results: list[ProbeResult]) -> list[ProbeResult]:
    return [result for result in results if not result.ok]


def build_probe_alert_payload(
    results: list[ProbeResult],
    *,
    alert_name: str = "InfraServiceProbeFailed",
    external_url: str = "infra2://platform/12.alerting/infra-probes",
    severity_override: str | None = None,
) -> dict:
    """Build an Alertmanager-shaped payload for the failing probes in ``results``.

    ``severity_override`` forces the severity of every alert AND the group severity —
    used to route "probe never succeeded" (misconfigured probe) failures to a low
    severity so a broken probe cannot page critical like a real service outage.
    """
    failures = failed_results(results)
    status = "firing" if failures else "resolved"
    alerts = [
        {
            "status": status,
            "labels": {
                "alertname": alert_name,
                "service": result.spec.name,
                "severity": severity_override or result.spec.severity,
                "probe_kind": result.spec.kind,
                "failure_domain": _failure_domain(result),
            },
            "annotations": {
                "summary": f"{result.spec.name} probe failed",
                "description": result.summary,
                "observed": result.observed,
            },
        }
        for result in failures
    ]
    if severity_override:
        severity = severity_override
    else:
        severity = failures[0].spec.severity if failures else "info"
    return {
        "status": status,
        "commonLabels": {
            "alertname": alert_name,
            "severity": severity,
            "team": "infra",
        },
        "commonAnnotations": {
            "summary": f"{len(failures)} infra service probe(s) failed"
            if failures
            else "All infra service probes recovered",
        },
        "groupLabels": {"alertname": alert_name},
        "alerts": alerts,
        "externalURL": external_url,
    }


def post_alert_bridge_payload(
    bridge_url: str,
    payload: dict,
    *,
    username: str = "",
    password: str = "",
    timeout: float = 10.0,
) -> dict:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if username or password:
        import base64

        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode(
            "ascii"
        )
        headers["Authorization"] = f"Basic {token}"
    request = Request(bridge_url, data=body, headers=headers, method="POST")
    with urlopen(request, timeout=timeout) as response:  # noqa: S310
        response_body = response.read().decode("utf-8")
    return json.loads(response_body) if response_body else {}


def _run_http(
    spec: ProbeSpec,
    *,
    http_get: Callable[[str, float], tuple[int, str]] | None,
) -> str:
    if http_get:
        status, body = http_get(spec.target, spec.timeout_seconds)
    else:
        request = Request(spec.target, headers=HTTP_PROBE_HEADERS, method="GET")
        try:
            with urlopen(request, timeout=spec.timeout_seconds) as response:  # noqa: S310
                status = response.status
                body = response.read(128).decode("utf-8", errors="replace")
        except HTTPError as exc:
            status = exc.code
            body = exc.read(128).decode("utf-8", errors="replace")
        except URLError as exc:
            raise RuntimeError(str(exc.reason)) from exc
    return f"{status}:{body.strip()}"


def _run_tcp(
    spec: ProbeSpec,
    *,
    tcp_connect: Callable[[str, int, float], None] | None,
) -> str:
    host, port_text = spec.target.rsplit(":", 1)
    port = int(port_text)
    if tcp_connect:
        tcp_connect(host, port, spec.timeout_seconds)
    else:
        with socket.create_connection((host, port), timeout=spec.timeout_seconds):
            pass
    return "connected"


def _run_command(
    spec: ProbeSpec,
    *,
    command_runner: Callable[[str, float], subprocess.CompletedProcess[str]] | None,
) -> str:
    if command_runner:
        result = command_runner(spec.target, spec.timeout_seconds)
    else:
        result = subprocess.run(
            shlex.split(spec.target),
            text=True,
            capture_output=True,
            timeout=spec.timeout_seconds,
            check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip() or result.stdout.strip() or "command failed"
        )
    return result.stdout.strip()


def _run_resource(spec: ProbeSpec, *, sample_seconds: float = 0.4) -> str:
    """Return host resource usage as a percentage string for cpu | mem | disk:<path>.

    Reads host-global /proc (/proc/stat and /proc/meminfo are not namespaced, so
    they report the host even from inside a container) and statvfs for disk.
    `disk:<path>` reports usage of the filesystem backing <path>; statvfs needs
    only ANY path on the target filesystem, so the deploy bind-mounts a small
    dedicated host directory (not the host root) into the container.
    """
    target = spec.target.strip()
    if target == "cpu":
        usage = _cpu_percent(sample_seconds)
    elif target == "mem":
        usage = _mem_percent()
    elif target.startswith("disk:"):
        usage = _disk_percent(target.split(":", 1)[1] or "/")
    else:
        raise ValueError(f"Unsupported resource target: {target!r}")
    return f"{usage:.1f}"


def _cpu_percent(sample_seconds: float) -> float:
    def snapshot() -> tuple[int, int]:
        with open("/proc/stat", encoding="utf-8") as handle:
            fields = handle.readline().split()
        values = [int(x) for x in fields[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)  # idle + iowait
        return sum(values), idle

    total0, idle0 = snapshot()
    time.sleep(max(0.05, sample_seconds))
    total1, idle1 = snapshot()
    total_delta = total1 - total0
    if total_delta <= 0:
        return 0.0
    busy = (1.0 - (idle1 - idle0) / total_delta) * 100.0
    return max(0.0, min(100.0, busy))


def _mem_percent() -> float:
    info: dict[str, int] = {}
    with open("/proc/meminfo", encoding="utf-8") as handle:
        for line in handle:
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts:
                info[key.strip()] = int(parts[0])  # kB
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", info.get("MemFree", 0))
    if total <= 0:
        return 0.0
    return max(0.0, min(100.0, (total - available) / total * 100.0))


def _disk_percent(path: str) -> float:
    stat = os.statvfs(path)
    total = stat.f_blocks * stat.f_frsize
    available = stat.f_bavail * stat.f_frsize
    if total <= 0:
        return 0.0
    return max(0.0, min(100.0, (total - available) / total * 100.0))


def _matches_expected(spec: ProbeSpec, observed: str) -> bool:
    if not spec.expected:
        return True
    if spec.kind == "http":
        status = observed.split(":", 1)[0]
        return status in {item.strip() for item in spec.expected.split(",")}
    if spec.kind == "resource":
        # expected is a percentage ceiling; pass while usage stays at or below it.
        try:
            return float(observed) <= float(spec.expected)
        except ValueError:
            return False
    return spec.expected in observed


def _failure_domain(result: ProbeResult) -> str:
    observed = result.observed.lower()
    summary = result.summary.lower()
    if any(
        marker in observed or marker in summary
        for marker in PROBE_CLIENT_BLOCKED_MARKERS
    ):
        return "probe-client-blocked"
    return "service-or-route"
