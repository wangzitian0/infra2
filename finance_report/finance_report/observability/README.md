# Finance Report — Observability config-as-code

> **Purpose**: Checked-in SigNoz alert rules and dashboard for finance_report (#373).
> **SSOT**: [docs/ssot/ops.observability.md](../../../docs/ssot/ops.observability.md)

This directory is the reviewable source of truth for the finance_report SigNoz
objects. Definitions are JSON; provisioning is a post-merge `invoke` apply step
(no manual clicks in the SigNoz UI).

## Files

| File | Purpose |
|------|---------|
| `alert_rules.json` | OTEL log-error rule, RED SLO and business-anomaly metric rules, and a telemetry-absence rule: `FinanceReportBackendErrorLogs`, `FinanceReportHigh5xxRate`, `FinanceReportP95LatencyHigh`, `FinanceReportStatementParseFailureSpike`, `FinanceReportRateLimitSaturation`, `FinanceReportAsyncTaskFailures`, and `FinanceReportBackendTelemetryAbsent`. The threshold, window, severity and no-data decision for each rule are in SOP-004C of [ops.observability.md](../../../docs/ssot/ops.observability.md). |
| `dashboard.json` | Baseline dashboard: backend error rate + latency, frontend web-vitals + exceptions. Stored in SigNoz's v5 query shape with a `layout`, and sent as the dashboard map itself (no `{"data": …}` wrapper). |
| `shared_tasks.py` | Idempotent apply/print invoke tasks, plus the read-only `verify-alert-routing` / `verify-dashboard` and the explicit `test-alert-channel`. |

The catalog has one top-level `service_id` and environment. Every generated SigNoz
rule receives the same low-cardinality `ServiceIdentity v1` labels, and log/metric
queries explicitly select `deployment.environment.name=production` so staging or
preview traffic cannot page a production rule.

## How the alert reaches Lark/Feishu

The rules route to the shared bridge channel, not to Feishu directly. Each rule
binds to the channel by **name** (`condition.thresholds.spec[].channels`): SigNoz
uses those strings as alertmanager receiver names, so a channel id there is never
delivered (#973). The name is defined once, in
`libs/alerting.py::signoz_feishu_channel_name`:

```text
finance-report-backend OTEL logs/metrics
  -> SigNoz finance_report alert rules
  -> SigNoz notification channel "infra2-feishu-alerts-<env>"
  -> http://platform-alerting${ENV_SUFFIX}:8080/signoz/webhook  (platform/12.alerting)
  -> Lark/Feishu group
```

The Feishu/Lark webhook secret lives only in 1Password
(`platform/{env}/alerting`) and is mirrored to Vault
`secret/platform/{env}/alerting` at deploy time. The SigNoz channel only ever
holds the internal bridge URL.

The metric rules are intentionally reviewed as config-as-code before live apply.
`FinanceReportRateLimitSaturation` depends on the app emitting
`finance_rate_limit_rejected`, and `FinanceReportAsyncTaskFailures` depends on
`finance_async_parse_failure`; applying before those app PRs deploy is harmless
but those two rules cannot fire until the metrics exist.

Metric alerts render as SigNoz v5 PromQL rules (`METRIC_BASED_ALERT` +
`promql_rule` with `condition.compositeQuery.queries`). The error-log rule renders
as a v5 builder `threshold_rule` whose logs query also sits in `queries[]`. The
apply task must exit non-zero if SigNoz rejects any checked-in rule. The live
SigNoz API still expects numeric threshold enums (`op` / `matchType`) inside that
v5 envelope.

SigNoz accepts a rule that can never fire, and it shows up as `inactive`. The
rules on `main` before #906 were all in that state: the PromQL rules selected
`http_server_request_count`-style names while SigNoz stores the dotted OTel names,
and the error-log rule was marked v5 but carried only the legacy `builderQueries`.
When you write a rule, follow the constraints in
§4.6 of [ops.observability.md](../../../docs/ssot/ops.observability.md).
The loader refuses bare metric selectors, `in_total` on range vectors and
`alert_on_absent`.

## Apply (post-merge)

```bash
# Prereqs: bridge deployed, SigNoz API key in Vault, channel ensured (SOP-004).
uv run python -m tools.deploy_v2 --service platform/alerting --type prod --iac-ref vX.Y.Z --domain zitian.party --code-reviewed
uv run python -m invoke signoz.shared.create-api-key

# Apply finance_report definitions (idempotent):
uv run python -m invoke fr-observability.shared.apply-alerts
uv run python -m invoke fr-observability.shared.apply-dashboard

# Stale managed rules and duplicate same-title dashboards are only REPORTED by default.
# Removing them is explicit (and owner-approved in production):
uv run python -m invoke fr-observability.shared.apply-alerts --prune
uv run python -m invoke fr-observability.shared.apply-dashboard --delete-duplicates

# Prove it (read-only), then send one real test notification through the channel:
uv run python -m invoke fr-observability.shared.verify-alert-routing
uv run python -m invoke fr-observability.shared.verify-dashboard
uv run python -m invoke fr-observability.shared.test-alert-channel

# Inspect payloads without touching SigNoz:
uv run python -m invoke fr-observability.shared.print-alerts
uv run python -m invoke fr-observability.shared.print-dashboard

# Live canary without applying the catalog:
uv run python tools/signoz_alert_rule_probe.py
```

## Verify (post-merge live gate)

1. Run `apply-observability.yml` with `mode=canary`; it creates one disabled
   temporary PromQL rule using the generated payload, verifies SigNoz stores the
   v5 `queries[]` envelope, then deletes it.
2. Emit more than 10 synthetic backend ERROR logs within 15 minutes (one is
   below the threshold by design); confirm `FinanceReportBackendErrorLogs` fires
   and a message lands in the Lark group.
3. Use `fr-observability.shared.print-alerts` to verify every rule renders with the
   channel name, `schemaVersion=v2alpha1` and a non-empty
   `condition.compositeQuery.queries` (`promql` for the metric rules,
   `builder_query` with `signal=logs` for `FinanceReportBackendErrorLogs`).
4. Run `verify-alert-routing`: it fails unless every managed rule is stored bound to
   the channel name and no managed rule is left over that the catalog dropped. It lists
   failures by kind: `unbound` / `missing` are fixed by `apply-alerts`, `stale` only by
   `apply-alerts --prune`. After the #973 fix, production needs that one `--prune` once
   (it removes `FinanceReportReconciliationAnomaly`, dropped from the catalog in #906),
   then `verify-alert-routing` must exit 0. Then run `test-alert-channel` and confirm the
   card in the Lark group. Expect a one-time burst: this is the first time SigNoz alerts
   can be delivered, so backlogged state (for example the 2026-10-01
   `FinanceReportBackendTelemetryAbsent` retry) may flush once.
5. In SigNoz, open each rule after apply. An `error` health state means SigNoz
   could not run the query; `inactive` is expected. If
   `FinanceReportBackendTelemetryAbsent` fires right after apply, the backend's
   request metrics are not arriving under the names this catalog selects.
6. Run `verify-dashboard`, then open the SigNoz dashboard "Finance Report — Backend &
   Frontend" and confirm the nine widgets render for `finance-report-backend` and
   `finance-report-frontend`.
