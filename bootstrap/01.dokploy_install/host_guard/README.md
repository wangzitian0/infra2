# Infra-022 host guard

The source of truth is [ops.pipeline.md §11.1](../../../docs/ssot/ops.pipeline.md#111-infra-022-宿主机快速防护与带外心跳). These files prepare the host changes; production acceptance requires evidence from the VPS and its external alert destination.

## Before production apply

1. Get owner approval for the **exact reviewed infra2 head SHA** under `AGENTS.md`.
2. Create three Healthchecks.io checks: host heartbeat (one-minute period), disk P1 (five-minute period), and disk P0 (five-minute period). Set their grace periods and notification routes so missed pings or `/fail` deliver within ten minutes. P0 must page immediately; P1 may use a daytime route. Keep the three ping URLs in `/etc/infra2/host-guard.env`, owned by root with mode `0600`, using [host-guard.env.example](./host-guard.env.example) as the assignment format. Never paste the real URLs into logs, commits, or review comments.
3. On the VPS, run `./configure_docker.sh --check` and `./install_host_guard.sh --check`; inspect `docker info --format '{{.LiveRestoreEnabled}}'`, `/etc/docker/daemon.json`, `systemctl status docker`, `df -P /data`, and current public routes. `--check` changes nothing.

## Apply and verify

Run `./configure_docker.sh --apply` and `./install_host_guard.sh --apply` from the approved checkout. The first script validates a merged candidate with `dockerd --validate`, saves the prior daemon file, reloads Docker, and checks live-restore. It rolls back the daemon file if reload or verification fails. The second installs the root-owned scripts and two systemd timers. The reload does not prove new logging defaults are active for existing containers. Test a newly created disposable container with `docker inspect -f '{{json .HostConfig.LogConfig.Config}}' <id>` and confirm `max-size=50m`, `max-file=3`; remove that disposable container. Do not recreate the single-replica Dokploy control plane just to change its old log config.

Verify both timers are enabled and have successful recent runs with `systemctl list-timers` and `journalctl -u infra2-disk-guardian.service -u infra2-host-heartbeat.service`. Confirm Docker and application routes stayed healthy after reload. Confirm all three external checks received pings. For alert delivery, stop only the heartbeat **timer** briefly and confirm the external missed-ping notification arrives within ten minutes, then restart it. Exercise the disk P1/P0 signal paths with a controlled command shim or disposable test mount rather than filling the production disk. Record the event receipt times and restore each external check to healthy. Never rely on a local script exit code as proof that the external notification arrived.

Docker daemon logging settings affect newly created containers only. Existing control-plane containers without per-container limits remain covered by the older six-hour host hygiene job and the five-minute disk guardian until a separately planned safe rebuild. No daemon restart or control-plane rebuild is part of this rollout.

## Host litter quarantine (`host_quarantine.sh`)

`./host_quarantine.sh` implements the two-phase host litter quarantine and 15-day delayed purge policy defined in [ops.storage.md SOP-003](../../../docs/ssot/ops.storage.md#sop-003-宿主机残留回收站隔离与延迟清理-host-quarantine--15-day-delayed-purge):

- `./host_quarantine.sh --check` scans known host litter paths, `/tmp/F3CNN` permission drift, and retired root cron jobs without mutating the host.
- `./host_quarantine.sh --stage` backs up and comments out retired root cron lines (`/root/backups/truealpha-prod-backup.sh` and `tianclaw-vps-disk-maintenance.sh`), moves litter into `/data/backups/quarantine/<batch-id>/files/`, writes `/data/backups/quarantine/manifest.jsonl` with a 15-day `purge_after_epoch`, and tightens `/tmp/F3CNN` permissions in place (`0700` dir, `0600` files).
- `./host_quarantine.sh --list` prints `/data/backups/quarantine/manifest.jsonl`.
- `./host_quarantine.sh --purge-expired` deletes only batches whose `purge_after_epoch` has passed and marks their manifest entries `"purged"`.
