# Infra-012: Watchdog Observability & Alert Completeness

> **Status**: In Progress (Phase 1 delivered, Phase 2/3 actively closing)  
> **Umbrella EPIC**: Infra-011  
> **Scope**: Make watchdog logs, failures, and dedupe decisions fully observable and traceable.

---

## Situation

## 2026-06-09 Execution Snapshot

Delivered in code:

- Retry + attempt-count logging in Cloudflare/GitHub watchdog checks.
- Structured watchdog events for run/check/complete and
  `watchdog.delivery.failure`.
- Cloudflare dedupe fingerprint stabilized to failure identity +
  `failure_domain`.
- GitHub fallback now opens a `watchdog-alert-fallback` issue when Feishu
  delivery fails.
- Weekly digest workflow (`ops-checks.yml`) sends 7-day summary of
  out-of-band watchdog run health.
- Alert text now includes `failure_domain`, suggested action, and runbook URL.

Still pending for full Infra-012 closure:

- Digest expansion from workflow-level reliability to full per-probe domain
  trend analysis in SigNoz.
- Extended Worker self-health metadata contract (`deployed_at`, config-hash
  drift, quota budget thresholds).

After PR #228 stabilized the Cloudflare watchdog dedupe fingerprint (reducing false 30-minute
alerts), the system became **stable enough to discover deeper gaps**:

1. **No logging on success**: Probes run every 60s (Dokploy layer) and 30min (CF layer), but only
   failures emit data. Cannot trace "route X was up all week" or calculate weekly availability
   statistics.

2. **No retry/buffer**: Single transient failure (network jitter, DNS hiccup) triggers immediate
   alert to Feishu. No distinction between "needs page now" and "probably recovers in 1 minute".
   Estimated **15-20% false-alert rate** from transient network events.

3. **No weekly digest**: Operators cannot see "failure distribution: 60% network, 30% resource,
   10% config" or identify patterns (e.g., "Redis flaps every Monday at 3 AM").

4. **Partial SSOT**: `docs/ssot/watchdog-signals.yaml` lists only 13 CF targets; missing Dokploy
   internal probes, GitHub checks. Not the single source of truth.

5. **Incomplete failure classification**: `failure_domain` (PR #228) is present but undocumented;
   no way to distinguish "resource exhaustion" from "network timeout" in probe output.

6. **No alert redundancy**: If Cloudflare Worker or alert bridge fails, **no fallback**
   notifies the operator until GitHub daily cron runs (worst case: 24h delay).

---

## Evidence: Why This Matters

### Incident: 30-Minute Alert Spam (May 2026)

**What happened**:
- User received ~24 identical "platform-alerting-probes stale" alerts over 4 hours
- Each alert claimed "heartbeat is 900s old", "heartbeat is 1200s old", etc.
- Root cause: Fingerprint hashed volatile `detail` field, so same logical failure appeared new every 30min

**Before fix**:
- Dedupe failed; 24 alerts sent to Feishu
- User called incident response
- Logs showed no root cause (no structured logging)

**After PR #228**:
- Fingerprint only includes `{env, name, failure_domain}`
- Same failure now dedupes → 1 alert sent
- User still can't see "what was the root cause?" or "how long did it last?"

**Lesson**: Dedupe stops alert **volume**, but we still lack **observability** into why alerts happened.

---

### Pain: Network Jitter vs Real Outage

**Scenario**: Cloudflare worker probes 9 routes every 30 minutes.
- If 1 in 1000 probes has a DNS timeout (transient), that's ~0.3 alerts/day = **false positive**
- Current behavior: Alert immediately → Feishu → operator checks → "it's up now" → alert closed

**Estimation**:
- Transient network event rate at global scale: ~1-2%
- 9 routes × 24 runs/day × 1-2% transient rate = **2-4 false alerts per day**
- If 50% of false alerts page operators at night = **1 unnecessary page per night**

**Ideal**: Retry after 1 minute. If still down, then alert. Expected result:
- False positive rate drops from 1-2% to <0.1% (only persistent failures alert)
- Operators sleep better

---

### Missing Data: Availability Timeline

**Current state**:
```
Jun-09 10:00 Alert: finance-report-api DOWN
Jun-09 10:30 (no message)
Jun-09 11:00 (no message)
Jun-09 11:30 Alert: finance-report-api DOWN (dedupe fired again)
```
User cannot answer: "Was the service down the whole time or did it recover briefly at 10:45?"

**Ideal state** (with success logging):
```
Jun-09 10:00 Log: finance-report-api attempt 1 FAIL (timeout)
Jun-09 10:01 Log: finance-report-api attempt 2 FAIL (HTTP 502)
Jun-09 10:02 Alert: finance-report-api DOWN (2 consecutive failures)
Jun-09 10:15 Log: finance-report-api attempt 3 SUCCESS (recovered)
Jun-09 10:30 Log: finance-report-api attempt 4 SUCCESS
Jun-09 11:00 Log: finance-report-api attempt 5 SUCCESS
Jun-09 11:30 Log: finance-report-api attempt 6 SUCCESS
```
User can now see: "Outage was 10:00-10:15 (15 min), root cause was HTTP 502 (likely upstream)."

---

## Redesigned Scope

### MECE Task Breakdown

| Slice | Components | Goal | Out of Scope |
|-------|-----------|------|--------------|
| **P0: Retry & Buffer** | Cloudflare Worker, GitHub watchdog | Implement 1-min retry; alert only on 2+ consecutive failures. Reduce false-alarm rate from 15-20% to <5%. | Changing probe interval (60s/30min stays) |
| **P0: Success Logging** | Dokploy probes, CF watchdog, GitHub watchdog | All probe runs (success + failure) emit structured JSON to SigNoz. Can query "route X uptime last 7 days". | Changing alerting rules or channels |
| **P0: SSOT Update** | `watchdog-signals.yaml`, `ops.alerting.md` | Expand SSOT to include all probes/targets; update cadence and failure_domain taxonomy. Code and docs must match. | Changing probe definitions |
| **P1: Weekly Digest** | Alert bridge, Lark integration | Every Monday 9 AM send: "Last week: 450 probe runs, 445 succeeded (98.9%), failures by domain: 60% network, 40% config". | Adding new alerting channels |
| **P1: Diagnostics** | Probe failure context logging | Record failure timestamp, retry count, duration, and suggested action in each alert. | Running diagnostics commands (SSH into host) |
| **P1: Signal Registry Completeness** | `watchdog-signals.yaml` | Register all 21 signals (13 CF + 2 heartbeat + 6 GitHub): no undocumented probes. | Changing probe logic |
| **P2: Worker Self-Health** | Cloudflare Worker `/health` | Detect Worker deployment errors, config syntax errors, KV rate-limit events within 1h. | Adding new Worker features |
| **P2: Alert Redundancy** | Feishu webhook + GitHub issue fallback | If alert bridge delivery fails, auto-create GitHub issue within 5 minutes. | Changing GitHub Actions integration |

### Dependencies

1. **P0 Retry & Buffer** must land before **P1 Diagnostics** (retry count needs to be logged).
2. **P0 Success Logging** must land before **P1 Weekly Digest** (digest needs historical data).
3. **P0 SSOT Update** unblocks all other slices (documentation first).

---

## Acceptance Criteria

| AC | Description | Proof of Effectiveness |
|----|-------------|------------------------|
| **Infra-012.1** | Implement retry logic for Cloudflare watchdog: on failure, retry after 1 minute, max 2 retries, alert only if all retries fail. | **Metric**: false-alarm rate drops from estimated 15-20% to <5% over first 7 days. Test case: simulate DNS timeout on first probe run, verify recovery on second attempt does not alert. |
| **Infra-012.2** | Implement retry logic for GitHub watchdog: on SSH/HTTP failure, retry after 30 seconds, max 2 retries, alert only if all retries fail. | **Metric**: GitHub daily logs show <2 alerts from transient network events per week. Test case: simulate SSH timeout, verify recovery retries do not alert. |
| **Infra-012.3** | All Dokploy internal probes (HTTP, TCP, command checks) emit structured JSON logs to SigNoz on every run (success + failure). Log schema includes: probe name, environment, timestamp, duration_ms, status (success/failure), result, failure_domain (if failed), retry_count. | **Query test**: Can run `SELECT probe, status, COUNT(*) FROM probe_logs WHERE timestamp > now() - INTERVAL '7 days' GROUP BY probe, status` and get full uptime timeline. Example: "finance-report-postgres: 10080 runs, 10075 success (99.95%), 5 failure (0.05%)". |
| **Infra-012.4** | Cloudflare watchdog emits success/failure logs to SigNoz on every 30-minute probe run. Same schema as AC-012.3. Logs queryable by route name, environment, failure_domain. | **Query test**: "Last 7 days, finance-report-api public route availability by environment." Results: prod 99.9%, staging 99.5%. |
| **Infra-012.5** | GitHub watchdog emits success/failure logs to SigNoz on every daily run. Same schema as AC-012.3. Includes worker health, SSH diagnostics, Docker health, alert bridge status. | **Query test**: Can retrieve weekly GitHub watchdog run summary. Example: "6 runs, 5 success, 1 failure: alert-bridge-health domain." |
| **Infra-012.6** | `docs/ssot/watchdog-signals.yaml` is updated to include all 21 active signals: 13 Cloudflare routes + 2 heartbeats + 6 GitHub checks. Schema includes signal_id, environment, component, cadence, severity, primary_owner, failure_domain_whitelist, renotify_window_sec, retry_count_max, alert_threshold. Validation test passes: no undocumented probes, no drift from code. | **Validation test** (`tools/watchdog_consistency_audit.py`): Scans code, finds all probes, cross-references `watchdog-signals.yaml`, reports 0 undocumented probes, 0 stale entries. |
| **Infra-012.7** | `docs/ssot/ops.alerting.md` updated: documents 3-tier architecture (Dokploy 60s, Dokploy internal?, CF 30min, GitHub 1day), cadence decision rationale, and failure_domain taxonomy (20+ values shared across CF + GitHub watchdogs). Includes SOP for "alert arrived, how do I debug?" | **Documentation test**: README readers can answer: (a) what cadence for each watchdog? (b) what failure_domain values exist? (c) how is retry configured? (d) what is expected alert volume per week? |
| **Infra-012.8** | Weekly digest job currently runs every Monday **01:00 UTC** via GitHub Actions (`ops-checks.yml`), aggregates the previous 7 days of out-of-band watchdog workflow runs, reviews structured watchdog logs for alert recall evidence (`watchdog.delivery.success`, `watchdog.delivery.failure`, fallback issue URLs, missing evidence), reports failure-domain distribution, and sends the reliability summary to Feishu. **Future phase**: extend digest source to SigNoz probe-level aggregation by component + failure_domain. | **Metric**: By second week, report identifies 1-2 recurring failure patterns from watchdog run outcomes and shows alert recall evidence for every alertable watchdog failure. Tests: `test_summarize_watchdog_log_events_counts_alert_recall_evidence`, `test_main_records_delivery_success_event_for_weekly_recall_audit`. |
| **Infra-012.9** | Enhanced diagnostics context: Probe failure alerts include suggested actions. Schema: `{ failure, domain, suggested_action, runbook_url }`. Example: "Alert: Dokploy API timeout. Action: SSH into infra2, run `docker logs platform-dokploy` to check for deploy loop. Runbook: link-to-ops-guide". | **Usability test**: On-call engineer reads alert, tries suggested action, says "yeah, that helped me find the bug faster." |
| **Infra-012.10** | Signal registry completeness: `watchdog-signals.yaml` has 21 signals, each with explicit cadence, renotify_window, retry_count, alert_threshold. CI job `lint_watchdog_signals.py` runs on every PR, enforces required fields, warns if new probe added without signal entry. | **CI test**: PR adding new probe without updating signals.yaml is blocked with error: "New probe 'foo-bar' not registered in watchdog-signals.yaml". |
| **Infra-012.11** | Cloudflare Worker self-health: Worker `/health` endpoint returns `{ ok: true, deployed_at, config_hash, kv_quota_pct }`. Monitoring job polls every 30 min, alerts if: deployed_at is old (> 24h stale), config_hash mismatch (config changed but Worker not redeployed), or kv_quota_pct > 90%. | **Metric**: Within 1 hour of Worker config error, alert reaches Feishu. Test case: manually push invalid Worker config, verify alert within 60 min. |
| **Infra-012.12** | Alert link redundancy: Cloudflare Worker failure notification includes retry logic for Feishu delivery. If Feishu webhook fails after 3 retries, fallback: auto-create GitHub issue with label `watchdog-alert-fallback`, title includes failure summary. GitHub Actions daily can read fallback issues, confirm watchdog liveness. | **Redundancy test**: Disable Feishu webhook URL, trigger fake CF watchdog failure, verify GitHub issue auto-created within 5 min. Re-enable Feishu, verify issue auto-closes. |
| **Infra-012.13** | Infra CI publishes a coverage context for `libs` and `tools` modules before introducing a fail-under threshold, the README exposes the main-branch Coveralls badge once main uploads begin, and high-risk deployment/client helper branches have smoke coverage before threshold ratcheting. | **CI test**: `infra-ci.yml` runs `pytest` with `pytest-cov`, uploads `infra2-coverage-context`, uploads Cobertura XML to Coveralls on `main`, keeps `--cov-fail-under=0` until the baseline is reviewed, and covers Dokploy client/deployer/CLI glue error paths without contacting real infrastructure. |
| **Infra-012.14** | SigNoz and OpenPanel have automated synthetic write-then-query probes: SigNoz writes an OTLP log nonce through the collector and queries `signoz_logs.distributed_logs_v2`; OpenPanel writes a `/track` event nonce and queries `openpanel.events`. | **CI tests**: `libs/tests/test_observability_roundtrip_probe.py` validates both emit/query payloads and `libs/tests/test_probe_specs.py::test_synthetic_roundtrip_canaries_are_declared` verifies the probes are declared. |
| **Infra-012.15** | The alert bridge→Feishu/Lark delivery path is proven **without** a periodic synthetic alert (the 6h real-send `alert-delivery-canary` was retired per #425 T3 — a periodic liveness proof implemented as a periodic alert is the anti-pattern #425 forbids). Coverage: `lark-delivery-http` (bridge config valid + Feishu reachable, no real post), the out-of-band watchdog's independent bridge `/health` check, the daily reports' own Feishu delivery, and real alerts when they fire. | **CI tests**: `libs/tests/test_probe_specs.py::test_synthetic_roundtrip_canaries_are_declared` asserts `lark-delivery-http` is declared and the real-send canary is gone. |

---

## Proof of Effectiveness

### Metric 1: Alert Volume Reduction

**Baseline** (current):
- 30-day alert volume: ~1000 Feishu messages
- Estimated breakdown: 80% true failures, 20% false positives (transient network, dedupe edge cases)
- False positive volume: ~200 alerts/month = 6-7 per day

**Target after P0 (retry + dedupe stabilization)**:
- Expected: ~850 total messages (150 less), with false positives <5% = <45/month
- **Success criteria**: Alert volume drops 15-20%, false positive rate <5%
- **Measurement window**: First 7 days post-deployment

### Metric 2: Observability

**Baseline**:
- Cannot query "what was uptime last week?"
- Cannot identify recurring failure patterns
- No structured failure taxonomy

**Target after P0 + P1 Success Logging + Weekly Digest**:
- Can query SigNoz: `uptime = (successful runs / total runs) × 100%` per component
- Can generate weekly digest with failure distribution
- Can identify: "Redis flaps every Monday 2-3 AM" (recurring pattern)
- **Success criteria**: First weekly digest identifies 2-3 actionable patterns

### Metric 3: SSOT Alignment

**Baseline**:
- `watchdog-signals.yaml` lists 13 CF targets; missing internal probes and GitHub checks
- Code and docs drift

**Target**:
- `watchdog-signals.yaml` lists all 21 active signals (100% coverage)
- CI validation blocks new probes without signal registry updates
- **Success criteria**: CI tool reports 0 drift on every PR

### Metric 4: Incident Response Speed

**Baseline**:
- Alert arrives: "Dokploy API timeout"
- Operator: "Is it network, resource, or config?" (guesses for 10+ min)
- Operator: SSH into host, runs `docker stats`, `df -h` manually

**Target**:
- Alert arrives: "Dokploy API timeout. Likely cause: resource exhaustion (prev run showed 85% mem). Action: docker logs platform-dokploy | grep OOM. Runbook: [link]"
- Operator: Tries suggested action, finds OOM error in 2 minutes

---

## Phased Rollout

### Phase 1: P0 Core (Target: Jun 20)

1. **Infra-012.1 & 012.2**: Retry logic + buffer in CF + GitHub watchdogs
2. **Infra-012.3-5**: Success logging to SigNoz
3. **Infra-012.6-7**: SSOT updates + validation

**Acceptance**: False-alarm rate drops to <5%, SSOT 100% complete, all logs queryable.

### Phase 2: P1 Observability (Target: Jul 10)

4. **Infra-012.8**: Weekly digest job
5. **Infra-012.9**: Enhanced diagnostics context
6. **Infra-012.10**: Signal registry CI enforcement

**Acceptance**: Weekly digest identifies failure patterns, new probes require signal entry.

### Phase 3: P2 Resilience (Target: Jul 31)

7. **Infra-012.11**: Cloudflare Worker self-health
8. **Infra-012.12**: Alert delivery redundancy

**Acceptance**: Worker errors detected within 1h, alert failure fallback to GitHub issues.

---

## Non-Goals

- Changing probe intervals (60s, 30min, 1day stay as-is)
- Replacing Feishu, Cloudflare, GitHub Actions, or SigNoz
- Weakening monitoring coverage
- Adding manual diagnostic commands (keep it push-only, not pull-only)

---

## References

- Prior: [Infra-011](./Infra-011.reliability_hardening.md) (stage contract + failure domains)
- Watchdog audit: [session files/watchdog_audit_report.md]
- Config: `docs/ssot/ops.alerting.md`, `docs/ssot/watchdog-signals.yaml`
- Code: `cloudflare/infra-watchdog/worker.js`, `tools/out_of_band_watchdog.py`, `platform/12.alerting/`
