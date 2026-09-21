# Infra2 CLI Tools

`tools/` holds two kinds of entry points — both are **entry points only**;
reusable logic belongs in `libs/` (see the division-of-labor note below):

1. **Invoke namespaces** — interactive CLI tasks loaded by `tools/loader.py`
   (`invoke <namespace>.<task>`).
2. **Standalone scripts** — non-interactive entry points run by CI gates or
   scheduled workflows (`python tools/<script>.py`).
   Examples: `deploy_v2.py` (deploy front door), `pre_deploy_schema_check.py`
   (pre-deploy enum & schema compatibility gate, #698), `deploy_guard_audit.py` /
   `ci_gate_audit.py` / `service_identity_audit.py` / `lint_platform_image_pins.py` /
   `coverage_regression_audit.py` (infra-ci
   gates), `reconcile_iac_inputs.py` (tag reconcile), `out_of_band_watchdog.py`
   / `watchdog_issue_trail.py` / `watchdog_weekly_digest.py` (scheduled
   watchdogs), `dns_drift_report.py` /
   `dokploy_config_drift.py` (drift reports), `pr_merge_gate.py` (the AGENTS.md
   session merge authority as a check: checks, threads, settling, protected and
   deploy-triggering paths; `--merge` squash-merges only a ready head; gh's
   "no checks reported" before the first check registers counts as zero checks),
   `orchestrator_guard_hook.py` (an owner-wired Claude Code hook that keeps the
   workspace orchestrator's foreground calls under 4 minutes and its watch armed;
   see [coordination.md](../harness/workspace/coordination.md#orchestrator-liveness)).

## The single resident entry point (#543)

`infra_probe_runner.py` is the ONE long-running process this repo ships
(container `platform-alerting-probes`). Everything resident runs inside its
loop: the minute-tier service probes (`INFRA_PROBE_SPECS`), the public-route
probes (`PUBLIC_ROUTE_PROBE_SPECS`), and the `ResidentWatcher` plugins
(`libs/resident_watchers.py` — container-breakdown watch and the deploy-queue
guard, each self-paced on its own interval, failure-isolated, and covered by
the runner's healthcheck/heartbeat).

Adding resident behavior means adding a watcher plugin in `libs/` and
registering it in `libs/resident_watchers.py::build_watchers` — NOT a new
sidecar, compose service, or standalone loop. A new alert path must register
its signal in `docs/ssot/watchdog-signals.yaml` (internal probe signals derive
automatically from the service's `ProbeFacet`/`SignalFacet` declarations);
`tools/no_new_wheels_lint.py` blocks CI otherwise.

`app_deploy_request.py` is the thin cross-repository adapter in front of `deploy_v2`.
`libs/app_deploy_request.py` deserializes `infra2_sdk.deploy.DeployRequest`, validates
source authority and immutable coordinates, remotely verifies Production run/review evidence,
and selects the released IaC ref; the tool only wires argv/env and invokes the existing deploy
front door. There is no CLI bypass for Production evidence. Dokploy/Vault mutation stays in
infra2. Its `markers` action needs no request: it prints the production marker next to the
newest release and exits non-zero once the marker is too old to pin the data engine from the
release being promoted (#650) — the ops-checks deploy-guard-audit job runs it daily.
`--require-preflight-canary` and `--preflight-canary-result` re-check the workflow's canary
pre-filter against the validated plan (only production requests are canaried, truealpha#860);
a disagreement in either direction fails before any credential is used. `deploy_v2` prints one
`deploy_v2 progress:` line per finished phase (secret supply, Dokploy promote, iac-runner sync)
on stderr, so the receiver log shows where a deploy's time went.

`webhook_delivery_audit.py` reads a repository's hook delivery list and fails only when
deliveries are being **attempted and none are landing** — the shape of #585, where
`/webhook` returned 401 on every delivery for seven weeks while the record of it went
unread. A quiet hook is not a finding and neither is one bad delivery among good ones.
It needs a token with `admin:repo_hook` read scope; the Actions token cannot carry that,
so its ops-checks step stays inert until `INFRA2_HOOKS_READ_TOKEN` is configured.

`ci_gate_audit.py` imports the released `infra2_sdk.ci` schema directly and validates
the infra-owned gate inventory against live workflow jobs. No local compatibility schema
or application source checkout participates in the audit.

`harness.py` is the read-only workspace front door. `check` validates
`harness/repos.yaml`, referenced authority files, the infra2/infra2-sdk focus, and the
autonomous App boundary. `status` reports parent pin, checkout/remote heads,
ahead/behind, dirty paths, and release identity; optional `--fetch` refreshes origin
metadata but never checks out/pulls submodules or changes application policy. `sweep`
classifies every item on the orchestrator's watch list (agents, PRs, release logs,
workflow runs, worktrees) into one state; logic in `libs/harness_sweep.py`. It only
prints, so it registers no alert signal (`tools/no_new_wheels_lint.py` scans for
alert-delivery calls; it makes none).

`dokploy_config_drift.py` is read-only and compares production's versioned,
secret-independent source fingerprint with the latest explicit `production/v*` promotion
marker. A missing marker is unknown desired state and fails closed instead of guessing from
the latest release. It first verifies that
the stored fingerprint can be reproduced from `IAC_DEPLOY_REF`; runtime secrets remain
only in the deploy idempotence hash. `--strict` fails on real drift, detector errors, and
structural mismatches while reporting pre-migration identity separately.

`service_identity_audit.py` is the blocking cross-plane identity gate. It validates
every registry service, all deployment entry points, checked-in alert catalogs,
and the complete root-owned internal/Cloudflare watchdog mapping; nested repositories and
tool-created hidden worktrees are outside this repository's identity authority.
`watchdog_consistency_audit.py`
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

Repository-specific architecture and verification entries are mapped in
[`harness/README.md`](../harness/README.md). Status verifies each checkout is its own
Git root before fetching; an empty optional submodule cannot refresh or report the
parent repository by accident.

```bash
# Human-readable result
uv run python -m tools.harness check

# Machine-readable result
uv run python -m tools.harness check --json

# Refresh remote metadata and report checkout/pin/release drift
uv run python -m tools.harness status --fetch

# Same observation, but fail when any checkout is ahead/behind/dirty/off-pin
uv run python -m tools.harness status --fetch --require-current

# One line per watched item, then `sweep: exit N`
uv run python -m tools.harness sweep /abs/path/watch.json

# Transitions and heartbeats only; exits when any item leaves WAITING (run under Monitor)
uv run python -m tools.harness sweep /abs/path/watch.json --watch
```

`sweep` is the orchestrator's clock ([Orchestrator Liveness](../harness/workspace/coordination.md#orchestrator-liveness)).
Each item gets exactly one state: `WAITING` (only time will move it), `DONE`, `ACTION`,
`STALL` (no progress past its threshold, or a dead process without a verdict line) or
`UNKNOWN` (a probe failed or returned a value outside the allow-lists). Merge gates are
judged by exit code only (0 ready, 2 owner); their output is discarded, and a gate argv
carrying `--merge`, `--request-review`, `--admin` or `--auto`, or an abbreviation argparse
would expand to one, is refused. Exit codes: 0 nothing needs you, 1 action, 2 an item
finished while others wait (watch only), 3 stall, 4 could not evaluate (including usage
and watch-list errors), 5 watch budget spent. `--interval` (90 s), `--heartbeat` (240 s),
`--max-minutes` (0 = never) and `--unknown-tolerance` (1 sweep) tune the watch.

The watch list is `{"items": [...]}`; every item may carry `label` and `stall_minutes`:

| `kind` | Fields | Reads |
|---|---|---|
| `pr` | `repo`, `number` or `head` (branch), optional `gate: {argv, cwd}` with `{number}` placeholder, `settle_minutes` (12) | `gh pr view`, `gh pr checks`, review threads (GraphQL), the gate's exit code |
| `workflow` | `repo`, `workflow`, optional `branch`, `event`, `expect_branch_head` | `gh run list`; with `expect_branch_head`, the run of the branch head's commit (only for workflows that run on every push) |
| `release_log` | `path`, optional `pid` or `process_match`, `done_lines`, `fail_prefixes` | the log file (an `exit=N` line decides first), `pgrep` |
| `agent` | `name`, `output` (the task output file) | `stat` only; the transcript is never opened |
| `worktree` | `path` | `git` branch, head, porcelain status, unpushed count; file mtimes |

```json
{"items": [
  {"kind": "pr", "repo": "wangzitian0/infra2", "number": 704,
   "gate": {"argv": ["uv", "run", "python", "-m", "tools.pr_merge_gate", "{number}", "--policy", "either"],
            "cwd": "/abs/path/to/infra2"}},
  {"kind": "workflow", "repo": "wangzitian0/infra2", "workflow": "Docs", "branch": "main"},
  {"kind": "agent", "name": "impl-1", "output": "/abs/path/to/tasks/impl-1.output"}
]}
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

## deploy_v2_canary.py

End-to-end proof for the unified deploy primitive. It deploys Finance Report to
the workflow-serialized `pr-999` slot, waits for the current trigger's terminal
Dokploy deployment record, verifies the exact requested version on every public
surface, and tears the stack and ephemeral database down in `finally`. Cleanup
passes only after two consecutive observations of absence. Success and failure
output include an `infra2_sdk.delivery.StageResult` and explicit `torn_down`
evidence. A same-repository PR passes its exact head SHA as `--iac-ref` and its
head branch as `--iac-clone-ref`; the two must resolve to the same commit before
Dokploy is changed. `--no-wait` is recorded as `skip`, never as a smoke pass.
The stage record's deadline is the configured `--timeout`; a functionally healthy
operation that exceeds it exits red as a `time-budget` failure instead of emitting
the contradictory `status=pass` / `budget_status=hard-breach` pair.
Scheduled/post-merge failures page through the out-of-band Feishu path, while PR
failures remain CI-only.

```bash
uv run python -m tools.deploy_v2_canary \
  --version-ref main \
  --iac-ref main \
  --domain zitian.party
```

## host_backup.sh

On-host nightly backup (installed as `/usr/local/sbin/infra2-host-backup.sh`,
cron in [SOP-006](../docs/ssot/ops.recovery.md#sop-006-on-host-scheduled-backup-runner-logical-dumps)).
Postgres dumps run first and busy path archives (minio) last. Every service is
attempted: a failing one is named on stderr as `FAILED <service_id>`, left out of
the manifest, and the run exits 1 without pruning old runs. `tar` exit 1 (a live
file changed mid-read) is a `WARN`, not a failure. Redis `SAVE` authenticates
with the container's own secrets and only `dump.rdb` is archived. Tests:
`libs/tests/test_host_backup_script.py` (PATH shims, no Docker needed).

```bash
ENV_SUFFIX=-staging BACKUP_OUTPUT_DIR=/tmp/hb tools/host_backup.sh
```

## backup_restore_rehearsal.py & run_restore_rehearsal.py

Guarded restore rehearsal for off-host backup artifacts. `run_restore_rehearsal.py`
automates the full sandbox lifecycle: spins up an ephemeral throwaway container
(zero host port binds, zero mount to `/data`, resource capped), downloads and
decrypts the latest artifact from Google Drive via `rclone crypt`, ingests the SQL
dump, verifies database and domain invariants, and cleanly tears down the container.

```bash
# Automated sandboxed end-to-end rehearsal (spins up sandbox, restores, asserts, destroys)
python tools/run_restore_rehearsal.py \
  --manifest /data/backups/infra2/manifest.json \
  --service-id finance_report/postgres \
  --database finance_report

# Drill every service in one scheduled run (each uses its own database, so
# --database is rejected here). One service failing never stops the rest: every
# service is attempted and the run exits 1 naming each failure (#618).
python tools/run_restore_rehearsal.py --service-id all
```

Each service emits its own `RESTORE_PROOF: PASS|FAIL` line; the exit code is the
contract (0 only when every drilled service passed). Invariants are per service —
`finance_report` asserts table/accounts/alembic floors, `truealpha` asserts table
count plus TOPT capture and GPPE row floors. The floors sit near half of the
observed production values: low enough not to flap on routine change, high enough
that a truncated restore cannot pass.

For direct manual invocation against an existing pre-provisioned rehearsal container:

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
SSH diagnostics, and consumes Dokploy's per-compose/application status as an
alert source (fail-closed `configuration` failure when `DOKPLOY_API_KEY` is
missing, #543). A Dokploy deploy error remains failed; when the independent
`infra2-docker-health` check is green it is routed as `state-discrepancy`/P2 for
reconciliation instead of being mislabeled as a confirmed runtime outage.
It is also the peer for truealpha's `scheduler-liveness` workflow
(`truealpha-scheduler-liveness`, logic in `libs/scheduler_peer_liveness.py`,
truealpha#876): red when that workflow is not active, has not ticked on schedule
within 2 x its largest cron gap + 1 h, never ticked while its file is older than
that bound, or cannot be read. `INFRA2_PEER_LIVENESS_BOUND_CAP_HOURS` only
tightens the bound (`0` = drill red). With `INFRA2_WATCHDOG_VERDICTS_PATH` set it
appends every verdict for the issue trail.

```bash
WATCHDOG_DRY_RUN=1 uv run python tools/out_of_band_watchdog.py
```

## watchdog_issue_trail.py

The ops-checks watchdog job's last step (truealpha#876 W4): reads the verdicts the
earlier steps recorded and keeps one issue per red check, titled exactly
`ops-checks watchdog is red: <check>` — open or comment while red, comment and
close when green in a scheduled run (or a plain dispatch on main). Drills and
branch dispatches never close; dry runs and SSH-override runs write nothing. A
failed listing never falls through to create; a failed write exits 1. Logic in
`libs/watchdog_issue_trail.py`.

```bash
GITHUB_EVENT_NAME=workflow_dispatch GITHUB_REF=refs/heads/feature \
  GITHUB_REPOSITORY=owner/sandbox GITHUB_TOKEN=... \
  INFRA2_WATCHDOG_VERDICTS_PATH=watchdog-verdicts.jsonl \
  uv run python tools/watchdog_issue_trail.py
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

## doc_link_check.py

Fails when a relative Markdown link in this repository points at nothing.
AGENTS.md builds the wiki on cross-references (the 0/1 級 entry map, the 互引原則,
every SSOT citation) and nothing verified them: 29 were dead when the check was
written. Run by [`docs.yml`](../.github/workflows/docs.yml) before the mkdocs build.

Three things are deliberately not failures, because each is real in a working
tree or in CI but absent from a plain checkout:

- **Submodules** — skipped by the paths `.gitmodules` registers, not by directory
  name. CI checks out non-recursively, so those directories are empty; name
  matching would also wrongly exclude this repository's own top-level
  `truealpha/` and `finance_report/`.
- **Generated, gitignored targets** — reported as an informational line. A
  sibling repository links at a generated `db-schema.md` from 13 places; every
  link is correct and a checker without this concept calls all 13 broken.
- **`KNOWN_UNRESOLVED`** — links whose target's existence is an open question,
  each recorded with the decision that is pending. A tracked question, never a
  guessed target.

```bash
python3 tools/doc_link_check.py
```

## References

- [文档索引](../docs/README.md)
- [SSOT Index](../docs/ssot/README.md)
- [Project Portfolio](../docs/project/README.md)
- [AI 行为准则](../AGENTS.md)
