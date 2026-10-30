# Infra2 Observability Domain Package (`common/observability`)

> **SSOT Domain**: In-band health probes, container breakdown log analysis, watchdog issue trail reconciliation, and resident watchers.

## Overview

`common/observability` provides health monitoring, crash diagnosis, and incident tracking for the `infra2` runtime:
1. **In-Band Health Probes**: Executes HTTP, TCP, and command probes declared by service `ProbeFacet` definitions to verify container liveness and dependencies.
2. **Container Breakdown Analysis**: Parses Docker daemon events and container stdout/stderr logs upon crash to classify failure modes (OOM, missing secrets, port conflicts, configuration error).
3. **Watchdog Issue Trail**: Reconciles red watchdog checks with GitHub Issues (truealpha#876 W4), ensuring exact-title deduplication, automatic resolution on green, and drill immutability.
4. **Resident Watchers**: Pluggable asynchronous monitoring tasks (`ContainerBreakdownWatcher`) executing within the alerting sidecar runner loop.

## Module Map

| Module | Role | Key Exports |
|--------|------|-------------|
| `probes.py` | In-band health probe specification & execution | `ProbeSpec`, `run_probe()`, `run_probes()`, `execute_probe()`, `parse_probe_specs()` |
| `probe_environment.py` | The one resolver of a probe process's environment; no default, raises when unset or unknown (#1208) | `resolve_probe_environment()`, `PROBE_ENVIRONMENT_VARIABLES` |
| `breakdown.py` | Container log triage & failure classification | `BreakdownVerdict`, `analyze_container_logs()`, `classify_reason()`, `build_breakdown_alert_payload()` |
| `issue_trail.py` | GitHub Issue trail reconciliation | `reconcile_issue_trail()`, `record_verdicts()`, `load_trail()`, `CheckVerdict`, `IssueApi`, `Trail` |
| `openpanel.py` | OpenPanel analytics client configuration SSOT | `OPENPANEL_CLIENTS`, `openpanel_env()` |
| `local_ledger.py` | VPS availability ledger: per-signal daily ok/fail counts and last ok/fail times written by the probe runner (#904, #1223) | `record_probe_round()`, `host_ledger_path()`, `to_report_days()` |
| `probe_specs.py` | Probe specs rendered from the registry for the probe runner | `render_probe_spec_text()`, `normalize_specs_text()` |
| `scheduler_peer_liveness.py` | Liveness of the peer scheduler workflow | `evaluate_peer_liveness()`, `BOUND_CAP_ENV` |
| `watchers/` | Alerting sidecar resident watcher plugins: `resident.py` (base and registry), `deploy_queue_guard.py`, `breakdown_watch.py` | `ResidentWatcher`, `build_watchers()`, `DeployQueueGuard`, `ContainerBreakdownWatcher` |

## Usage Examples

### Executing In-Band Probes
```python
from common.observability import ProbeSpec, run_probe

spec = ProbeSpec(
    name="postgres-tcp",
    kind="tcp",
    target="platform-postgres:5432",
    timeout_sec=5.0,
)

result = run_probe(spec)
if result.status != "ok":
    print(f"Probe failed: {result.detail} (latency: {result.latency_ms}ms)")
```

### Triaging Container Crashes
```python
from common.observability import analyze_container_logs

verdict = analyze_container_logs("platform-alerting", "ERROR: connection refused")
print(f"Container breakdown cause: {verdict.cause} ({verdict.detail})")
```

### Reconciling Watchdog Issues
```python
from pathlib import Path
from common.observability.issue_trail import (
    CheckVerdict,
    GhIssueApi,
    load_trail,
    reconcile_issue_trail,
    record_verdicts,
)

trail = load_trail(Path("/tmp/watchdog-trail.json"))
record_verdicts(
    trail,
    [
        CheckVerdict(name="s3_health", ok=True, detail="S3 200 OK"),
        CheckVerdict(name="vault_seal", ok=False, detail="Vault is sealed"),
    ],
    now_iso="2026-10-30T00:00:00Z",
)
# Opens/comments/closes corresponding GitHub issues with exact title matching
reconcile_issue_trail(GhIssueApi(repo="wangzitian0/infra2"), trail, mode="live")
```

## Observability Invariants

- **Cascade Suppression**: Downstream probe failures (e.g. app failing because postgres is dead) are suppressed if the root dependency is failing.
- **Deduplication**: One GitHub issue per unique watchdog failure signature; never flood the issue tracker on transient flaps.
- **Guards & Tests**: Covered by `common/tests/test_infra_probes.py`, `common/tests/test_container_breakdown.py`, and `common/tests/test_watchdog_issue_trail.py`.
