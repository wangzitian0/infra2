#!/usr/bin/env bash
# Dokploy deploy-queue watchdog.
#
# The Dokploy "deployments" queue is single-concurrency FIFO (BullMQ) with NO
# execution timeout. A deploy whose worker dies or orphans leaves a job stuck in
# the active list with an EXPIRED lock, which blocks ALL further deploys
# indefinitely; BullMQ's own stalled-check does not reliably recover it. This
# clears only such stalled jobs so the queue advances. It never touches a job
# that is actively processing (its lock is renewed every ~15s), so legitimate
# long deploys (image pulls, migrations) are safe.
#
# Condition to clear an ACTIVE job:
#   - lock TTL <= 0  (worker is not holding/renewing it -> orphaned/stalled)
#
# This is the ONLY safe signal. BullMQ sets the job's lock atomically when it
# moves the job to `active` (moveToActive Lua), so a missing/expired lock means
# the worker genuinely stopped renewing it. We deliberately do NOT use an
# age/`processedOn` ceiling: `processedOn` is written a moment AFTER the lock is
# set, so a just-picked-up job can briefly show proc=0 and a bogus huge age,
# which would wrongly kill a healthy deploy.
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
    rc LREM bull:deployments:active 0 "$id" >/dev/null
    rc DEL "bull:deployments:${id}" "bull:deployments:${id}:lock" >/dev/null
    echo "$(date -u +%FT%TZ) cleared stalled deploy job ${id}: lock-expired(ttl=${ttl}s)" >> "$LOG"
  fi
done
