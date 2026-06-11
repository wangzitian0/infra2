#!/usr/bin/env bash
# Dokploy deploy-queue watchdog — OBSERVABILITY ONLY.
#
# The Dokploy "deployments" queue is single-concurrency FIFO (BullMQ) with NO
# execution timeout. A deploy whose worker dies or orphans leaves a job stuck in
# the active list with an EXPIRED lock, which blocks ALL further deploys
# indefinitely; BullMQ's own stalled-check does not reliably recover it.
#
# This script DETECTS that condition and logs it. It does NOT mutate the queue.
# A watchdog observes; it must not take destructive action. Remediation (killing
# a stuck deploy, draining the queue) lives in the deploy-queue-guard sidecar
# (platform/12.alerting), which acts through Dokploy's OWN API
# (compose.killBuild / cancelDeployment / cleanQueues) rather than raw Redis
# surgery — that keeps BullMQ's bookkeeping and the deployment records consistent.
#
# Detected (read-only) signal for a stalled ACTIVE job:
#   - lock TTL <= 0  (worker is not holding/renewing it -> orphaned/stalled)
# A job actively processing renews its lock every ~15s, so legitimate long
# deploys (image pulls, migrations) are never flagged.
#
# Installed to /usr/local/sbin/ and run every minute via root cron by
# `invoke dokploy_install.install-deploy-watchdog` (part of dokploy_install.setup).
set -uo pipefail

RED="$(docker ps --format '{{.Names}}' | grep -m1 dokploy-redis || true)"
[ -n "$RED" ] || exit 0
rc() { docker exec "$RED" redis-cli "$@" 2>/dev/null; }

LOG="${DEPLOY_WATCHDOG_LOG:-/var/log/dokploy-deploy-watchdog.log}"

for id in $(rc LRANGE bull:deployments:active 0 -1); do
  [ -n "$id" ] || continue
  ttl="$(rc TTL "bull:deployments:${id}:lock")"; ttl="${ttl:--2}"
  case "$ttl" in (''|*[!0-9-]*) ttl=0 ;; esac

  if [ "$ttl" -le 0 ]; then
    echo "$(date -u +%FT%TZ) DETECTED stalled deploy job ${id}: lock-expired(ttl=${ttl}s) — remediation is owned by the deploy-queue-guard sidecar" >> "$LOG"
  fi
done
