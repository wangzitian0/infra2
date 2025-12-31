# E2E Regression Testing Framework

> **Role**: Infrastructure & Application Verification
> **Engine**: Pytest + Playwright + UV

This framework verifies that the actual state of the infrastructure matches the architectural definitions in SSOT.

## 📚 SSOT References

For the authoritative test strategy and pyramid, refer to:
> [**E2E Regression SSOT**](../docs/ssot/ops.e2e-regressions.md)

## 🧭 Navigation

- [Docs Index](../docs/README.md)
- [SSOT Index](../docs/ssot/README.md)
- [Project Portfolio](../docs/project/README.md)
- [AI 行为准则](../AGENTS.md)

## 📂 Test Suites

| Suite | Purpose | SSOT Anchor |
|-------|---------|-------------|
| `bootstrap/` | Dokploy, DNS, TLS, /data mounts. | [Bootstrap SSOTs](../docs/ssot/README.md#bootstrap---引导层) |
| `platform/` | Vault + Authentik availability. | [Platform SSOTs](../docs/ssot/README.md#platform---平台层) |
| `data/` | Database connectivity. | [Data SSOTs](../docs/ssot/README.md#data---数据层) |
| `smoke/` | Critical path verification (Fast). | [E2E SSOT / Smoke](../docs/ssot/ops.e2e-regressions.md#测试分级-test-pyramid) |

## 🚦 Usage

### Setup
```bash
cd e2e_regressions
uv sync

# Configure env (export in shell/CI)
# See e2e_regressions/.env.example for the required keys
# If INTERNAL_DOMAIN is missing, tests will try 1Password init/env_vars (requires `op` signed in).
```

### Execution
```bash
# Run smoke tests (from e2e_regressions/)
uv run pytest tests/smoke/ -v

# Run platform tests (from e2e_regressions/)
uv run pytest tests/platform/ -v

# From repo root
uv run pytest e2e_regressions/tests/smoke/ -v
uv run pytest e2e_regressions/tests/platform/ -v
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
