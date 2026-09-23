# Infra-022 P0 on-call runbooks

Use this page for the five failure classes named by [#723](https://github.com/wangzitian0/infra2/issues/723). Each path starts with a 30-second read-only check, then has at most three actions. Record the incident time, environment, exact release SHA and the command output. Production changes, including a release rollback, still require the owner approval described in [AGENTS.md](../../AGENTS.md). Do not run a destructive command just to make an alert green.

| Symptom | First place to look | Runbook |
|---|---|---|
| `/data` at 85% or higher | `df -P /data` | [Disk full](#disk-full) |
| Deploy failed or queue stuck | CI run and Dokploy deployment record | [Deployment failed](#deployment-failed) |
| Container exited, OOM killed or restart loop | `docker inspect` state and exit code | [Container killed](#container-killed) |
| Schema gate rejects the release | Gate result and `ROLLBACK_CLASS` | [Schema divergence](#schema-divergence) |
| Worker, host or probe heartbeat stops | External check receipt and Worker `/status` | [Watchdog silent](#watchdog-silent) |

## Disk full

**Symptom:** `/data` crosses 80% (P1) or 85% (P0); Docker writes or database commits may fail.

**30 seconds:** On the VPS, run `df -P /data` and `systemctl status infra2-disk-guardian.timer --no-pager`; if the new timer is not installed yet, inspect the existing host hygiene schedule. Check the external disk P1/P0 check receipts. Do not infer free space from an old dashboard.

1. Identify growth without deleting data: `du -xhd1 /data | sort -h` and `docker system df`. Confirm that the backup directory is not the cause of a misconfigured retention policy before touching it.
2. At ≥80%, run only the approved safe cleanup if the guard failed to run: `docker image prune -f --filter dangling=true` and `docker builder prune -f --filter until=24h`. Recheck `df -P /data`. Do not prune volumes, running containers, in-use images, preview resources or backups.
3. At ≥85% after cleanup, page P0 and inspect `journalctl -u infra2-disk-guardian.service -n 80 --no-pager`. Follow the guardian's oversized json-log path only after confirming the target is a Docker json log; do not truncate database, Vault, MinIO or backup files. Recheck writes and external alert delivery.

**Brake / recovery:** If the new guardian itself misbehaves, stop its timer with `systemctl stop infra2-disk-guardian.timer` after owner approval, keep the external disk check active, and restore the prior approved script. Space recovered is not data recovered; use [recovery SOP](../ssot/ops.recovery.md) for damaged state.

## Deployment failed

**Symptom:** deploy CI fails, Dokploy deployment remains `running` beyond 30 minutes, or a release probe fails.

**30 seconds:** Run `gh run view <run-id> --repo wangzitian0/infra2` and compare its exact SHA, environment and service set with the Dokploy deployment record. Do not retry a different SHA while the first attempt is unresolved.

1. Stop further promotion and classify the failure domain from the failed stage: preflight/schema, image, IaC Runner, Dokploy apply, or post-deploy probe. Use the [pipeline SSOT](../ssot/ops.pipeline.md) and inspect the original run/deployment IDs.
2. For a stuck queue, inspect `DeployQueueStuck` evidence and the Dokploy record before using its supported cancel/clean path. Never delete BullMQ/Redis keys to force the queue green. If runtime and control plane disagree, follow the [state discrepancy protocol](../ssot/ops.standards.md#rule-4-状态不一致协议-state-discrepancy-protocol).
3. Restore service with a reviewed, exact prior release only if the gate reports `ROLLBACK_CLASS: A` and its data contract is backward compatible. For Class C, hold the release and forward-fix; `migrate down` is forbidden. Verify live probes and keep the failed SHA in the incident record.

**Brake / recovery:** Cancel an in-flight CI run with `gh run cancel <run-id> --repo wangzitian0/infra2` if it has not applied production changes. There is deliberately no generic production rollback command for an unknown migration class; use the approved exact-release procedure in [ops.pipeline](../ssot/ops.pipeline.md) after classifying data compatibility.

## Container killed

**Symptom:** `ContainerBreakdown`, OOM kill, nonzero exit or restart loop.

**30 seconds:** Run `docker inspect -f '{{.State.Status}} {{.State.ExitCode}} {{.State.OOMKilled}}' <container>` and `docker logs --tail 80 <container>` on the VPS. Check `df -P /data` and `free -h` to distinguish host pressure from one bad release.

1. Identify the service and environment from the container labels and the latest Dokploy deployment; preserve logs and exit code before any restart.
2. If host memory/disk is exhausted, use the relevant host runbook first. If a new release caused the crash, halt promotion and classify rollback safety before restoring a reviewed prior release through the normal deploy path.
3. If neither explains it, inspect healthcheck and secret rendering, then escalate to the service owner. Verify the container stays healthy and its public or internal probe passes; a single successful `docker start` is not closure.

**Brake / recovery:** Do not issue `docker restart` to an unknown single-replica stateful service or delete its volume. Use the service's approved deploy/recovery SOP; preserve the original container and data evidence until the restore path is known.

## Schema divergence

**Symptom:** `pre_deploy_schema_check.py` blocks with missing/extra enum labels, `NOT EVALUATED`, or `ROLLBACK_CLASS: C`.

**30 seconds:** Open the exact deploy run's Schema Gate output and read service, environment, `missing_in_db`, `missing_in_code`, and `ROLLBACK_CLASS`. `NOT EVALUATED` is a blocked check, not a passing schema.

1. Pause the release before Dokploy changes. Compare the reviewed app metadata and the target DB's actual enum labels using the [Schema Gate contract](../project/Infra-022.production_resilience_and_dr.md#l3-发布门禁闭环与告警降噪-deploy--observability); never copy a Staging result into a Production decision.
2. If the code is ahead and migration is additive, review and apply the forward migration through the normal release gate. If DB has labels code removed or renamed, classify C and plan a forward-compatible fix; do not run `migrate down`.
3. Re-run the same gate on the same exact image and target environment. Record the gate result and only then resume promotion.

**Brake / recovery:** Stop promotion; there is no safe generic `--force-promote` or reverse-migration command in the current implementation. Revert the unpromoted code commit with `git revert <commit>` if needed, then submit and review a new release. A production database already changed requires a forward fix and owner decision.

## Watchdog silent

**Symptom:** Host heartbeat missed, Cloudflare Worker dead-man check missed, or the in-band probe heartbeat goes stale.

**30 seconds:** Inspect the external Healthchecks.io receipt time and the Worker `/status` result using its authenticated status token. Compare with the latest `.github/workflows/ops-checks.yml` run. A green in-band SigNoz dashboard cannot prove the host is alive.

1. If the VPS is unreachable, use the provider console and [recovery SOP](../ssot/ops.recovery.md) to establish host, Docker, disk and network state; keep the external P0 open until an independent route is healthy.
2. If the VPS is healthy but the Worker check is stale, inspect Cloudflare cron/logs and its deployment SHA. If only the host timer is stale, inspect `systemctl status infra2-host-heartbeat.timer --no-pager` and `journalctl -u infra2-host-heartbeat.service -n 80 --no-pager` without exposing the ping URL.
3. Restore the failed scheduler or network path under the approved exact head, then prove both a fresh external ping and a real notification drill. Confirm the Worker and VPS checks remain separate.

**Brake / recovery:** Inspect `.github/workflows/deploy-cloudflare-watchdog.yml` at the exact commit before acting. While it has a `push` trigger for `cloudflare/infra-watchdog/**`, merging Worker code deploys Production and requires owner approval of that PR head. If the workflow is manual-only with an `approved_sha` input, obtain owner approval of the current `main` SHA, then dispatch with `gh workflow run deploy-cloudflare-watchdog.yml --repo wangzitian0/infra2 --ref main -f approved_sha=<sha>`. Do not substitute an old SHA or suppress the external check to clear a page.
