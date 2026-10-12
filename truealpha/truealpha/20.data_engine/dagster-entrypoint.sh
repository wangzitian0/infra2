#!/bin/sh
set -eu

# Tests set SECRETS_ENV_FILE. A deployment never sets it, so the default is the Vault mount.
secrets_file="${SECRETS_ENV_FILE:-/secrets/.env}"
max_wait=60
waited=0
while [ ! -s "$secrets_file" ] && [ "$waited" -lt "$max_wait" ]; do
  sleep 2
  waited=$((waited + 2))
done
[ -s "$secrets_file" ] || { echo "FATAL: Vault secrets were not rendered after ${max_wait}s"; exit 1; }

set -a
# shellcheck source=/dev/null
. "$secrets_file"
set +a

export DAGSTER_POSTGRES_URL="${DATABASE_URL}?options=-csearch_path%3Ddagster"

# Boot validation (#1210). Refuse to start when the process lacks a variable that the image
# manifest declares as injected. The validator checks names only and never prints values.
# A failure exits non-zero here, so "set -e" stops the script before "exec".
python -c "from truealpha_runtime.boot import assert_environment; assert_environment('apps/data-engine/required-env.generated.json', require_injected=True)"

exec "$@"
