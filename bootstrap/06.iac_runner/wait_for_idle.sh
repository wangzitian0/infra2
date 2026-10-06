# shellcheck shell=bash
#
# Wait until the running IaC Runner reports no in-flight deploys (#666).
#
# scripts/deploy_iac_runner_bootstrap.sh sources this file after it checks out
# bootstrap/06.iac_runner at INFRA2_DEPLOY_SHA, so this helper always matches that script.
# A recreate drops the runner's in-memory deploy state, so the script calls
# wait_for_runner_idle after the image build and immediately before the recreate.
#
# The workflow streams the bootstrap script to `bash -s` over ssh. A command here that
# reads stdin would consume the rest of that script. Every docker call reads a heredoc
# or /dev/null.
#
# Settings (environment):
#   IAC_RUNNER_IDLE_WAIT_SECONDS  wait limit in whole seconds (default 900)
#   IAC_RUNNER_IDLE_POLL_SECONDS  seconds between polls (default 10)
#
# The wait never blocks the self-update: an unknown count or the limit continues.

_IAC_RUNNER_IDLE_CONTAINER="iac-runner"

# Print the runner's in-flight deploy count, or print nothing when it is unknown.
_iac_runner_in_flight_count() {
  docker exec -i "$_IAC_RUNNER_IDLE_CONTAINER" python - <<'PY'
import json
import os
import urllib.error
import urllib.request

# The compose healthcheck uses the same listener. Only a test sets the override; docker
# exec does not pass the host environment into the container.
url = os.environ.get("IAC_RUNNER_HEALTH_URL", "http://127.0.0.1:8080/health")
try:
    with urllib.request.urlopen(url, timeout=20) as response:
        body = response.read()
except urllib.error.HTTPError as exc:
    # A degraded runner answers 503 with the same JSON body.
    body = exc.read()
except Exception:
    raise SystemExit(0)
try:
    value = json.loads(body).get("in_flight_deploys")
except Exception:
    raise SystemExit(0)
if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
    print(value)
PY
}

wait_for_runner_idle() {
  local limit="${IAC_RUNNER_IDLE_WAIT_SECONDS:-900}"
  local interval="${IAC_RUNNER_IDLE_POLL_SECONDS:-10}"
  local whole_number='^[0-9]+$'
  local poll_number='^[0-9]+([.][0-9]+)?$'
  local running count started elapsed

  if ! [[ "$limit" =~ $whole_number ]]; then
    echo "IaC Runner idle wait: IAC_RUNNER_IDLE_WAIT_SECONDS='$limit' is not a whole number; using 900"
    limit=900
  fi
  if ! [[ "$interval" =~ $poll_number ]]; then
    echo "IaC Runner idle wait: IAC_RUNNER_IDLE_POLL_SECONDS='$interval' is not a number; using 10"
    interval=10
  fi

  running="$(
    docker inspect --format '{{.State.Running}}' "$_IAC_RUNNER_IDLE_CONTAINER" \
      </dev/null 2>/dev/null
  )" || running=""
  if [ "$running" != "true" ]; then
    echo "IaC Runner idle wait: container $_IAC_RUNNER_IDLE_CONTAINER is not running; no in-flight deploy to wait for"
    return 0
  fi

  started="$(date +%s)"
  while :; do
    count="$(_iac_runner_in_flight_count)" || count=""
    elapsed=$(($(date +%s) - started))
    if ! [[ "$count" =~ $whole_number ]]; then
      echo "IaC Runner idle wait: in-flight deploy count is unknown (no in_flight_deploys in /health; an older runner image does not report it); continuing without a wait"
      return 0
    fi
    echo "IaC Runner idle wait: $(date -u +%Y-%m-%dT%H:%M:%SZ) in_flight_deploys=$count elapsed=${elapsed}s limit=${limit}s"
    if [ "$count" -eq 0 ]; then
      echo "IaC Runner idle wait: no in-flight deploys; continuing to the recreate"
      return 0
    fi
    if [ "$elapsed" -ge "$limit" ]; then
      echo "WARNING: runner still reports $count in-flight deploy(s) after ${elapsed}s; rebuilding anyway"
      return 0
    fi
    sleep "$interval"
  done
}
