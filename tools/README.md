# Infra2 CLI Tools

`tools/` holds two kinds of entry points — both are **entry points only**;
reusable logic belongs in `common/` (see the division-of-labor note below):

1. **Invoke namespaces** — interactive CLI tasks loaded by `tools/loader.py`
   (`invoke <namespace>.<task>`).
2. **Standalone scripts** — non-interactive entry points run by CI gates or
   scheduled workflows (`python tools/<script>.py`).
   Examples: `deploy_v2.py` (deploy front door), `pre_deploy_schema_check.py`
   (pre-deploy enum & schema compatibility gate, #698), `deploy_guard_audit.py` /
   `ci_gate_audit.py` / `service_identity_audit.py` / `lint_compose_resource_limits.py` /
   `coverage_regression_audit.py` (infra-ci
   gates), `reconcile_iac_inputs.py` (tag reconcile; its `--phase pre` is the
   prod-promotion guard that reads stage records, #1646), `stage_records.py` (the record reader),
   `out_of_band_watchdog.py`
   / `watchdog_issue_trail.py` / `watchdog_weekly_digest.py` (scheduled
   watchdogs), `dokploy_config_drift.py` (drift reports), `pr_merge_gate.py` (the AGENTS.md
   session merge authority as a check: checks, threads, settling, protected and
   deploy-triggering paths, plus GitHub's own `mergeable`/`mergeStateStatus`
   and a stale-green guard -- green proves the tree the checks ran on, so a
   sibling merge that rewrites this PR's files invalidates it without any
   check going red; `--merge` squash-merges only a ready head; gh's
   "no checks reported" before the first check registers counts as zero checks),
   `orchestrator_guard_hook.py` (an owner-wired Claude Code hook that keeps the
   workspace orchestrator's foreground calls under 4 minutes and its watch armed;
   see [coordination.md](../harness/workspace/coordination.md#orchestrator-liveness)), and
   `ci_runner.py` (unified local and CI gate runner supporting `--changed` for
   diff-scoped fail-fast checks with local worker caps `<= 4`).

## The single resident entry point (#543)

`infra_probe_runner.py` is the ONE long-running process this repo ships
(container `platform-alerting-probes`). Everything resident runs inside its
loop: the minute-tier service probes (`INFRA_PROBE_SPECS`), the public-route
probes (`PUBLIC_ROUTE_PROBE_SPECS`), and the `ResidentWatcher` plugins
(`common/observability/watchers/resident.py` — container-breakdown watch and the deploy-queue
guard, each self-paced on its own interval, failure-isolated, and covered by
the runner's healthcheck/heartbeat).

Adding resident behavior means adding a watcher plugin in `common/` and
registering it in `common/observability/watchers/resident.py::build_watchers` — NOT a new
sidecar, compose service, or standalone loop. A new alert path must register
its signal in `docs/ssot/watchdog-signals.yaml` (internal probe signals derive
automatically from the service's `ProbeFacet`/`SignalFacet` declarations);
`tools/no_new_wheels_lint.py` blocks CI otherwise.

`reconcile_iac_inputs.py` orchestrates the unified multi-app and platform release train across
`reconcile-iac-inputs.yml`. It detects changed `iac_pinned` platform services and App `release_tag`
or `10.app/**` updates in `harness/repos.yaml`, emits per-service `deploy_v2` commands for Apps and
their declared `companions` (`truealpha/data_engine`) while excluding companions from the platform
batch, and executes App runtime hooks (`infra2_sdk.manifests.run_hook_slot`) and release evidence
checks (`common/deploy/app_contract.py`). `common/deploy/release_markers.py` (`python -m common.deploy.release_markers`)
prints the production marker next to the newest release and exits non-zero once the marker is too
old to pin the data engine from the release being promoted (#650) — the ops-checks
deploy-guard-audit job runs it daily. `deploy_v2` prints one `deploy_v2 progress:` line per finished
phase (secret supply, Dokploy promote, iac-runner sync) on stderr.

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
workflow runs, worktrees) into one state; logic in `common/core/harness/sweep.py`. It only
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

## Division of labor (`common/` vs `tools/`)

- **`common/`** — importable, unit-testable logic (no `__main__`, no argv).
- **`tools/`** — thin entry points: argv parsing, env wiring, exit codes.
  A tool that grows real logic should push it down into `common/` so it gets
  covered by `common/tests` (pattern: `tools/deploy_guard_audit.py` →
  `common/deploy/dependencies.py`). Several older scripts still carry embedded logic —
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
- Output uses `common.console` helpers; avoid raw `print` in new tasks.
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

# Also read the release each App serves in staging and production (read-only HTTPS GET)
# and the infra2 release tags. `unreachable` marks a failed read. `unknown` marks an empty one.
uv run python -m tools.harness status --fetch --online

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

## host_backup.sh

On-host weekly backup (installed as `/usr/local/sbin/infra2-host-backup.sh`,
cron in [SOP-006](../docs/ssot/ops.recovery.md#sop-006-on-host-scheduled-backup-runner-logical-dumps)).
Postgres dumps run first and busy path archives (s3) last. Every service is
attempted: a failing one is named on stderr as `FAILED <service_id>`, left out of
the manifest, and the run exits 1 and keeps its local run directory. `tar` exit 1 (a live
file changed mid-read) is a `WARN`, not a failure. Redis `SAVE` authenticates
with the container's own secrets and only `dump.rdb` is archived. Tests:
`common/tests/test_host_backup_script.py` (PATH shims, no Docker needed).
The script covers every declared `BackupFacet`, removes its own local run
directory after the off-host copy is verified equal (#1624), and writes separate
`production-manifest.json` and `staging-manifest.json` latest pointers.

```bash
ENV_SUFFIX=-staging BACKUP_OUTPUT_DIR=/tmp/hb tools/host_backup.sh
```

## run_restore_rehearsal.py

Guarded restore rehearsal for off-host backup artifacts (`common/backup/rehearsal.py`). `run_restore_rehearsal.py`
automates the full sandbox lifecycle: spins up an ephemeral throwaway container
(zero host port binds, zero mount to `/data`, resource capped), downloads and
decrypts the latest artifact from Google Drive via `rclone crypt`, ingests the SQL
dump, verifies database and domain invariants, and cleanly tears down the container.

```bash
# Automated sandboxed end-to-end rehearsal (spins up sandbox, restores, asserts, destroys)
python tools/run_restore_rehearsal.py \
  --manifest /data/backups/infra2/production-manifest.json \
  --service-id finance_report/postgres \
  --database finance_report

# Drill every service in one scheduled run (each uses its own database, so
# --database is rejected here). One service failing never stops the rest: every
# service is attempted and the run exits 1 naming each failure (#618).
python tools/run_restore_rehearsal.py --service-id all

# Explicit Staging drill; Production is the default and rejects this manifest.
python tools/run_restore_rehearsal.py \
  --environment staging \
  --manifest /data/backups/infra2/staging-manifest.json \
  --service-id all
```

Each service emits its own `RESTORE_PROOF: PASS|FAIL` line; the exit code is the
contract (0 only when every drilled service passed). Invariants are per service —
`finance_report` asserts table/accounts/alembic floors, `truealpha` asserts table
count plus TOPT capture and GPPE row floors. The floors sit near half of the
observed production values: low enough not to flap on routine change, high enough
that a truncated restore cannot pass.

## out_of_band_watchdog.py

The daily out-of-band audit, run from GitHub Actions outside the infra2 host
(ops.observability.md SOP-005B). It pages Feishu directly only for the failure
classes the GitHub layer owns (#908), and only when the set of paged checks changes
(#962, `common/observability/page_dedup.py`; the same set later is a `[报告] 仍未恢复`
in the reports chat, an emptied set one RESOLVED page): Cloudflare Worker liveness (`/status`
freshness against the Worker's own `WATCHDOG_STATUS_MAX_AGE_SECONDS`), the
production backup and the restore rehearsal, the peer scheduler, and failures of
its own configuration. The Worker counts as live only if its last run is fresh
and delivered its alerts. Which checks only report is read from
`docs/ssot/watchdog-signals.yaml` (GitHub signals with `type: report`); every
other check pages, including targets added only through repository variables.
The report-only checks — host reachability, SSH, Docker, container health, the
alert bridge, the staging backup, Dokploy per-compose/application status — still
run and go into one daily report via `common/alerting/delivery.py::deliver_infra2_report`. A
Dokploy `error` is listed there as a stale record only when that unit's own
containers (compose project = `appName`, read over SSH) run healthy on the
current `IAC_CONFIG_HASH`; otherwise it stays a failure, labelled with its age
past 72 h. A missing or rejected (401/403) `DOKPLOY_API_KEY`, or a missing report
secret, is a `configuration` failure and pages.
It also checks every scheduled workflow of the estate repositories
(`estate-scheduler-liveness`, logic in `common/observability/estate_scheduler_liveness.py`,
#1432): red when a scheduled workflow is disabled, has not ticked on
schedule within 2 x its own largest cron gap + 1 h, never ticked while its file is
older than that bound, or cannot be read. `INFRA2_PEER_LIVENESS_BOUND_CAP_HOURS` only
tightens the bound (`0` = drill red). With `INFRA2_WATCHDOG_VERDICTS_PATH` set it
appends the paging checks' verdicts for the issue trail (report-only checks open
no issues).

```bash
WATCHDOG_DRY_RUN=1 uv run python tools/out_of_band_watchdog.py
```

## watchdog_issue_trail.py

The ops-checks watchdog job's last step (truealpha#876 W4): reads the verdicts the
earlier steps recorded and keeps one issue per red paging check (and, when every
step recorded completely, closes issues of checks nobody records any more), titled exactly
`ops-checks watchdog is red: <check>` — open or comment while red, comment and
close when green in a scheduled run (or a plain dispatch on main). Drills and
branch dispatches never close; dry runs and SSH-override runs write nothing. A
failed listing never falls through to create; a failed write exits 1. Logic in
`common/observability/issue_trail.py`.

```bash
GITHUB_EVENT_NAME=workflow_dispatch GITHUB_REF=refs/heads/feature \
  GITHUB_REPOSITORY=owner/sandbox GITHUB_TOKEN=... \
  INFRA2_WATCHDOG_VERDICTS_PATH=watchdog-verdicts.jsonl \
  uv run python tools/watchdog_issue_trail.py
```

## local (local readiness + bootstrap)

- 输出统一使用 `common.console`（状态行 + 命令块），不直接 `print`。

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
`common/security/vault_audit.load_inventory`).

It also reports (never fails on) whether any field in a service's
`optional_inert_fields` SecretsFacet entry (`common/core/facets.py`) is
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

## fetch_app_manifests.py

Guards against vendored App environment manifests reappearing in `contracts/` (#1320).
A deploy renders the Vault Agent template directly from the deployed release manifest merged with the stack baseline manifest (`env.manifest.json`).

```bash
uv run python tools/fetch_app_manifests.py                           # offline guard check; infra-ci runs it
```

The deploy receiver checks non-Vault compose fields against the stack manifest and renders Vault-backed fields from the release manifest (`common/deploy/app_contract.py`). See [`bootstrap.vars_and_secrets`](../docs/ssot/bootstrap.vars_and_secrets.md).

## References

- [文档索引](../docs/README.md)
- [SSOT Index](../docs/ssot/README.md)
- [Project Portfolio](../docs/project/README.md)
- [AI 行为准则](../AGENTS.md)
