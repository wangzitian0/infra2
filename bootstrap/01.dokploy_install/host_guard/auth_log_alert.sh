#!/usr/bin/env bash
# Scan /var/log/auth.log for abnormal SSH authentication spikes and emit OTLP log to SigNoz (#724-R2).
set -euo pipefail

AUTH_LOG="${INFRA2_AUTH_LOG_PATH:-/var/log/auth.log}"
STATE_DIR="${INFRA2_AUTH_ALERT_STATE_DIR:-/var/lib/infra2}"
OFFSET_FILE="${STATE_DIR}/auth_log.offset"
OTEL_URL="${INFRA2_OTEL_LOGS_URL:-http://127.0.0.1:14318/v1/logs}"
THRESHOLD="${INFRA2_SSH_FAIL_THRESHOLD:-5}"

[[ -f "${AUTH_LOG}" ]] || exit 0
mkdir -p "${STATE_DIR}"

current_size="$(wc -c < "${AUTH_LOG}" | tr -d ' ')"
last_offset=0
if [[ -f "${OFFSET_FILE}" ]]; then
  last_offset="$(cat "${OFFSET_FILE}" 2>/dev/null || echo 0)"
fi
if [[ ! "${last_offset}" =~ ^[0-9]+$ ]] || (( current_size < last_offset )); then
  last_offset=0
fi

new_bytes=$(( current_size - last_offset ))
echo "${current_size}" > "${OFFSET_FILE}"
(( new_bytes > 0 )) || exit 0

failures="$(tail -c +"$(( last_offset + 1 ))" "${AUTH_LOG}" | grep -Ec 'sshd\[[0-9]+\]: (Failed password|Invalid user|maximum authentication attempts exceeded|Disconnecting authenticating user)' || true)"
(( failures >= THRESHOLD )) || exit 0

ts_ns="$(date +%s)000000000"
payload="$(cat <<EOF
{"resourceLogs":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"infra-host-ssh"}},{"key":"deployment.environment","value":{"stringValue":"production"}}]},"scopeLogs":[{"logRecords":[{"timeUnixNano":"${ts_ns}","severityNumber":17,"severityText":"ERROR","body":{"stringValue":"HostSshAuthFailureSpike: ${failures} SSH authentication failures in /var/log/auth.log"}}]}]}
EOF
)"

curl -fsS -m 5 -X POST "${OTEL_URL}" -H "Content-Type: application/json" -d "${payload}" >/dev/null || true
