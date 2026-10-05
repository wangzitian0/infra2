# Infra2 Observability Domain Package (`libs/observability`)

> **SSOT Domain**: In-band health probes, container breakdown log analysis, watchdog issue trail reconciliation, and resident watchers.

## Overview

`libs/observability` provides health monitoring, crash diagnosis, and incident tracking for the `infra2` runtime:
1. **In-Band Health Probes**: Executes HTTP, TCP, and command probes declared by service `ProbeFacet` definitions to verify container liveness and dependencies.
2. **Container Breakdown Analysis**: Parses Docker daemon events and container stdout/stderr logs upon crash to classify failure modes (OOM, missing secrets, port conflicts, configuration error).
3. **Watchdog Issue Trail**: Reconciles red watchdog checks with GitHub Issues (truealpha#876 W4), ensuring exact-title deduplication, automatic resolution on green, and drill immutability.
4. **Resident Watchers**: Pluggable asynchronous monitoring tasks (`ContainerBreakdownWatcher`) executing within the alerting sidecar runner loop.

## Module Map

| Module | Role | Key Exports |
|--------|------|-------------|
| `probes.py` | In-band health probe specification & execution | `ProbeSpec`, `execute_probe()`, `run_probes()`, `parse_probe_specs()` |
| `breakdown.py` | Container log triage & failure classification | `BreakdownVerdict`, `analyze_container_logs()`, `classify_reason()`, `build_breakdown_alert_payload()` |
| `issue_trail.py` | GitHub Issue trail reconciliation | `reconcile_watchdog_issues()`, `record_verdicts()`, `load_trail()`, `CheckVerdict` |
| `openpanel.py` | OpenPanel analytics client configuration SSOT | `OPENPANEL_CLIENTS`, `openpanel_env()` |
| `local_ledger.py` | VPS availability ledger: per-signal daily ok/fail counts written by the probe runner (#904) | `record_probe_round()`, `host_ledger_path()`, `to_report_days()` |
| `watchers/` | Alerting sidecar resident watcher plugins | `ContainerBreakdownWatcher`, `BreakdownWatch`, `sweep_breakdowns()` |

## Usage Examples

### Executing In-Band Probes
```python
from libs.observability import execute_probe, ProbeSpec

spec = ProbeSpec(
    name="postgres-tcp",
    kind="tcp",
    target="platform-postgres:5432",
    timeout_seconds=5,
    severity="critical",
)

result = execute_probe(spec)
if not result.ok:
    print(f"Probe failed: {result.message} (latency: {result.latency_ms}ms)")
```

### Triaging Container Crashes
```python
from libs.observability import analyze_container_logs

verdict = analyze_container_logs("platform-alerting", tail_lines=100)
if verdict.is_broken:
    print(f"Container broken: {verdict.reason} -> Suggested remediation: {verdict.remediation}")
```

### Reconciling Watchdog Issues
```python
from libs.observability import reconcile_watchdog_issues, CheckVerdict

verdicts = [
    CheckVerdict(check="minio_health", ok=True, summary="MinIO 200 OK"),
    CheckVerdict(check="vault_seal", ok=False, summary="Vault is sealed"),
]
# Opens/comments/closes corresponding GitHub issues with exact title matching
reconcile_watchdog_issues(verdicts, repo="wangzitian0/infra2")
```

## Observability Invariants

- **Cascade Suppression**: Downstream probe failures (e.g. app failing because postgres is dead) are suppressed if the root dependency is failing.
- **Deduplication**: One GitHub issue per unique watchdog failure signature; never flood the issue tracker on transient flaps.
- **Guards & Tests**: Covered by `libs/tests/test_infra_probes.py`, `libs/tests/test_container_breakdown.py`, and `libs/tests/test_watchdog_issue_trail.py`.
