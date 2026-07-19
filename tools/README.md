# Infra2 CLI Tools

`tools/` holds two kinds of entry points — both are **entry points only**;
reusable logic belongs in `libs/` (see the division-of-labor note below):

1. **Invoke namespaces** — interactive CLI tasks loaded by `tools/loader.py`
   (`invoke <namespace>.<task>`).
2. **Standalone scripts** — non-interactive entry points run by CI gates,
   scheduled workflows, or as long-running sidecars (`python tools/<script>.py`).
   Examples: `deploy_v2.py` (deploy front door), `deploy_guard_audit.py` /
   `ci_gate_audit.py` / `service_identity_audit.py` / `lint_platform_image_pins.py` /
   `coverage_regression_audit.py` (infra-ci
   gates), `reconcile_iac_inputs.py` (tag reconcile), `out_of_band_watchdog.py`
   / `watchdog_weekly_digest.py` (scheduled watchdogs), `deploy_queue_guard.py`
   (alerting-stack sidecar), `dns_drift_report.py` / `dokploy_config_drift.py`
   (drift reports).

`app_deploy_request.py` is the thin cross-repository adapter in front of `deploy_v2`.
`libs/app_deploy_request.py` deserializes `infra2_sdk.deploy.DeployRequest`, validates
source authority and immutable coordinates, remotely verifies Production run/review evidence,
and selects the released IaC ref; the tool only wires argv/env and invokes the existing deploy
front door. There is no CLI bypass for Production evidence. Dokploy/Vault mutation stays in
infra2.

`ci_gate_audit.py` imports the released `infra2_sdk.ci` schema directly and validates
the infra-owned gate inventory against live workflow jobs. No local compatibility schema
or application source checkout participates in the audit.

`harness.py` is the read-only workspace front door. It validates
`harness/repos.yaml`, referenced authority files, the infra2/infra2-sdk focus, and the
autonomous App boundary. It never updates submodules or application policy.

`dokploy_config_drift.py` is read-only and compares production's versioned,
secret-independent source fingerprint with the latest release. It first verifies that
the stored fingerprint can be reproduced from `IAC_DEPLOY_REF`; runtime secrets remain
only in the deploy idempotence hash. `--strict` fails on real drift, detector errors, and
structural mismatches while reporting pre-migration identity separately.

`service_identity_audit.py` is the blocking cross-plane identity gate. It validates
every registry service, all deployment entry points, checked-in alert catalogs,
and the complete internal/Cloudflare watchdog mapping. `watchdog_consistency_audit.py`
also enforces compose↔inventory equality and registry-derived `service_id` values.

## Division of labor (`libs/` vs `tools/`)

- **`libs/`** — importable, unit-testable logic (no `__main__`, no argv).
- **`tools/`** — thin entry points: argv parsing, env wiring, exit codes.
  A tool that grows real logic should push it down into `libs/` so it gets
  covered by `libs/tests` (pattern: `tools/deploy_guard_audit.py` →
  `libs/deploy_dependencies.py`; `tools/lint_platform_image_pins.py` →
  `libs/image_pins.py`). Several older scripts still carry embedded logic —
  treat that as debt to sink, not a pattern to copy.

## Runner (invoke namespaces)

- Use `invoke` inside an activated venv, or prefix with `uv run` when using uv.
- List all tasks: `invoke --list` (未激活虚拟环境时用 `uv run invoke --list`).

## Invoke namespaces

| Namespace | Entry | Purpose |
|-----------|-------|---------|
| `env` | `tools/env_tool.py` | Remote env/secret SSOT operations |
| `dokploy` | `tools/dokploy_env.py` | Dokploy project/environment helpers |
| `local` | `tools/local_init.py` | Local CLI checks and bootstrap helpers |
| `vault-audit` | `tools/vault_audit.py` | Read-only Vault app-token self-refresh audit |

## Common Conventions

- Pattern: `invoke <namespace>.<task>`
- `env` defaults: `--env=production`, `--service` optional (required for `list-all`)
- Write operations use `KEY=VALUE` (quote values with spaces)
- Output uses `libs.console` helpers; avoid raw `print` in new tasks.
- Omit `--service` for environment-level (`{project}/{env}`) values.

## harness.py

```bash
# Human-readable result
uv run python -m tools.harness check

# Machine-readable result
uv run python -m tools.harness check --json
```

## env (remote secrets)

Remote-first secrets operations (1Password/Vault). No local `.env` sync.

```bash
# Read secret
invoke env.get KEY --project=platform --service=postgres

# Write secret
invoke env.set KEY=VALUE --project=platform --service=postgres

# List secrets (masked)
invoke env.list-all --project=platform --service=postgres

# Show init/env_vars from 1Password
invoke env.init-status
```

## dokploy (project/environment)

```bash
# List environments for a project
invoke dokploy.env-list --project=platform

# Ensure staging environment exists
invoke dokploy.env-ensure --project=platform --env=staging --description="staging env"
```

## dokploy_route_canary.py

Dynamic route materialization proof for the Dokploy platform. It deploys a
minimal same-host web/API compose and returns JSON that classifies failures as
control plane, compose source-type drift, deployment record/worker, Docker
runtime, or public Traefik route failures.

```bash
python tools/dokploy_route_canary.py \
  --host route-canary.zitian.party \
  --environment-id="$DOKPLOY_ENVIRONMENT_ID" \
  --project platform \
  --env staging \
  --dokploy-host cloud.zitian.party \
  --repair-stale-compose
```

`--repair-stale-compose` is restricted to `route-canary*` hosts and
`dokploy-route-canary*` compose names. Repaired composes are normalized back to
`sourceType=raw` before redeploying. GitHub canary runs default to the stable
canary host/compose and rely on workflow concurrency to avoid overlap.

## deploy_v2_canary.py

End-to-end proof for the unified deploy primitive. It deploys Finance Report to
the reserved `pr-999` slot, verifies public health, and tears the stack and
ephemeral database down in `finally`. Success output and failure alerts include
an `infra2_sdk.delivery.StageResult` whose target contains the resolved code and
IaC SHAs; `--no-wait` is recorded as `skip`, never as a smoke pass.
Scheduled/post-merge failures page through the out-of-band Feishu path, while
PR failures remain CI-only.

```bash
uv run python -m tools.deploy_v2_canary \
  --version-ref main \
  --iac-ref main \
  --domain zitian.party
```

## backup_restore_rehearsal.py

Guarded restore rehearsal for off-host backup artifacts. The command verifies the
manifest freshness/checksum contract, refuses live-looking targets by default,
downloads remote artifacts through `rclone copyto`, restores into the rehearsal
container, and runs an invariant SQL check.

```bash
uv run python tools/backup_restore_rehearsal.py \
  --manifest /var/backups/finance_report/backup-manifest.json \
  --service-id finance_report/postgres \
  --target-container finance-report-postgres-rehearsal \
  --download-dir /tmp/restore-rehearsal \
  --database finance_report \
  --invariant-sql "select count(*) from alembic_version"
```

## out_of_band_watchdog.py

Direct Feishu watchdog intended to run outside the infra2 host from GitHub
Actions. It verifies public host reachability, Cloudflare Worker self-health,
SSH diagnostics, and the Dokploy route canary.

```bash
INFRA2_WATCHDOG_DRY_RUN=1 uv run python tools/out_of_band_watchdog.py
```

## local (local readiness + bootstrap)

- 输出统一使用 `libs.console`（状态行 + 命令块），不直接 `print`。

```bash
# Check CLI dependencies
invoke local.check

# Guide local setup (prints install instructions)
invoke local.init

# Show installed CLI versions
invoke local.version

# Validate init/env_vars in 1Password (no local .env)
invoke local.bootstrap

# Detect current bootstrap phase
invoke local.phase
```

## vault-audit (Vault runtime proof)

Read-only audit for the Vault app-token self-refresh chain. It checks Dokploy
env, Vault token lookup, rendered `/vault/secrets/.env` freshness, vault-agent
logs, and container state for every service in the facet-derived inventory
(#542: each service Deployer's `SecretsFacet` declarations ->
`libs/vault_self_refresh_audit.load_inventory`).

It also reports (never fails on) whether any field in a service's
`optional_inert_fields` SecretsFacet entry (`libs/service_facets.py`) is
actually populated in the rendered secrets file, not just wired -- e.g.
`finance_report/app`'s `LLM_ENCRYPTION_KEYS`, which can have a valid
`secrets.ctmpl` render line while Vault still holds no value, silently
leaving the feature it unlocks inert (#526). These show up as `INFO P3
<service>::optional-field-inertness::<FIELD>` lines in the report.

```bash
# Live production audit
invoke vault-audit.self-refresh

# Audit one inventory row
invoke vault-audit.self-refresh --service=finance_report/app

# Machine-readable output
invoke vault-audit.self-refresh --json-output

# Offline classifier test from a captured observation fixture
invoke vault-audit.self-refresh --observations=/path/to/observations.json
```

## References

- [文档索引](../docs/README.md)
- [SSOT Index](../docs/ssot/README.md)
- [Project Portfolio](../docs/project/README.md)
- [AI 行为准则](../AGENTS.md)
