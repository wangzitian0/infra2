#!/bin/sh
set -eu

max_wait=60
waited=0
while [ ! -s /secrets/.env ] && [ "$waited" -lt "$max_wait" ]; do
  sleep 2
  waited=$((waited + 2))
done
[ -s /secrets/.env ] || { echo "FATAL: Vault secrets were not rendered after ${max_wait}s"; exit 1; }

set -a
. /secrets/.env
set +a

export DAGSTER_POSTGRES_URL="${DATABASE_URL}?options=-csearch_path%3Ddagster"
exec "$@"
