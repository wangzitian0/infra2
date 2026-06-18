# Finance Report — Observability config-as-code

> **Purpose**: Checked-in SigNoz alert rules and dashboard for finance_report (#373).
> **SSOT**: [docs/ssot/ops.alerting.md](../../../docs/ssot/ops.alerting.md) ·
> [docs/ssot/ops.observability.md](../../../docs/ssot/ops.observability.md)

This directory is the reviewable source of truth for the finance_report SigNoz
objects. Definitions are JSON; provisioning is a post-merge `invoke` apply step
(no manual clicks in the SigNoz UI).

## Files

| File | Purpose |
|------|---------|
| `alert_rules.json` | OTEL log-error rule plus RED SLO and business-anomaly metric rules: `FinanceReportBackendErrorLogs`, `FinanceReportHigh5xxRate`, `FinanceReportP95LatencyHigh`, `FinanceReportStatementParseFailureSpike`, `FinanceReportReconciliationAnomaly`, `FinanceReportRateLimitSaturation`, and `FinanceReportAsyncTaskFailures`. |
| `dashboard.json` | Baseline dashboard: backend error rate + latency, frontend web-vitals + exceptions. |
| `shared_tasks.py` | Idempotent apply/print invoke tasks. |

## How the alert reaches Lark/Feishu

The rules route to the shared bridge channel, not to Feishu directly:

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

## Apply (post-merge)

```bash
# Prereqs: bridge deployed, SigNoz API key in Vault, channel ensured (SOP-004).
uv run python -m invoke alerting.setup
uv run python -m invoke signoz.shared.create-api-key

# Apply finance_report definitions (idempotent):
uv run python -m invoke fr-observability.shared.apply-alerts
uv run python -m invoke fr-observability.shared.apply-dashboard

# Inspect payloads without touching SigNoz:
uv run python -m invoke fr-observability.shared.print-alerts
uv run python -m invoke fr-observability.shared.print-dashboard
```

## Verify (post-merge live gate)

1. Emit a synthetic backend ERROR log; confirm `FinanceReportBackendErrorLogs`
   fires and a message lands in the Lark group.
2. Use `fr-observability.shared.print-alerts` to verify the six `#1106` rules
   render with a channel id and `schemaVersion=v2alpha1`.
3. Open the SigNoz dashboard "Finance Report — Backend & Frontend" and confirm
   the four widgets render for `finance-report-backend` and
   `finance-report-frontend`.
