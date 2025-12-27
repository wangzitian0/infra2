# E2E Regression Testing Framework

> **Role**: Infrastructure & Application Verification
> **Engine**: Pytest + Playwright + UV

This framework verifies that the actual state of the infrastructure matches the architectural definitions in SSOT.

## 📚 SSOT References

For the authoritative test strategy and pyramid, refer to:
> [**E2E Regression SSOT**](../docs/ssot/ops.e2e-regressions.md)

## 📂 Test Suites

| Suite | Purpose | SSOT Anchor |
|-------|---------|-------------|
| `bootstrap/` | Core cluster, network, and storage. | [Bootstrap SSOTs](../docs/ssot/README.md#bootstrap---引导层) |
| `platform/` | Identity, Secrets, and Control Plane. | [Platform SSOTs](../docs/ssot/README.md#platform---平台层) |
| `data/` | Database connectivity and auth. | [Data SSOTs](../docs/ssot/README.md#data---数据层) |
| `smoke/` | Critical path verification (Fast). | [E2E SSOT / Smoke](../docs/ssot/ops.e2e-regressions.md#测试分级-test-pyramid) |

## 🚦 Usage

### Setup
```bash
cd e2e_regressions
uv sync
```

### Execution
```bash
# Run smoke tests
uv run pytest tests/smoke/ -v

# Run platform tests
uv run pytest tests/platform/ -v
```

## 📊 Scoring

> **Method**: Weighted sum (weights can be tuned by team goals).

```
TotalScore = Stability + Coverage + Speed + Diagnosability + Recoverability
```

### Stability (30)

```
StabilityScore = 30 * (1 - FlakyFailureRate) - 30 * NonProductFailureRate
```

- **FlakyFailureRate**: flaky failures / total runs
- **NonProductFailureRate**: failures caused by infra, data, environment, or tooling (exclude product defects)

### Coverage (25)

```
CoverageScore = 25 * RiskCoverageRate
```

- **RiskCoverageRate**: covered risk items / total risk items
- **Prerequisite**: risk list must be structured (owned, tagged, and tracked)

### Speed (20)

- **P95 Duration** scoring buckets (example):
  - `<30min`: 20
  - `30-60min`: 15
  - `60-120min`: 10
  - `>120min`: 5 (or lower per team goals)

### Diagnosability (15)

```
DiagnosabilityScore = 15 * SelfDiagnosableRate
```

- **SelfDiagnosableRate**: failures with root cause identified without manual SSH or log spelunking / total failures

### Recoverability (10)

```
RecoverabilityScore = 10 * (0.5 * AutoCleanupSuccessRate + 0.5 * RollbackOrRerunSuccessRate)
```

- **AutoCleanupSuccessRate**: automatic cleanup succeeds / cleanup attempts
- **RollbackOrRerunSuccessRate**: rollback or rerun succeeds / attempts (idempotency)

---

## ⚠️ /e2e Command Issue

- **Issue**: `/e2e` comment did not trigger the E2E workflow
- **Cause**: `issue_comment` event was triggered by a bot due to GitHub App token context
- **Fix**: dispatch `e2e-tests.yml` via `workflow_dispatch` with the infra-flash App token

---
*Last updated: 2025-12-26*
