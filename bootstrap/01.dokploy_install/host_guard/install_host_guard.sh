#!/usr/bin/env bash
# Install two independent systemd timers after their root-only URLs exist.
set -euo pipefail

case "${1:---check}" in
  --check|--apply) MODE="${1:---check}" ;;
  *) echo "usage: $0 [--check|--apply]" >&2; exit 2 ;;
esac
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
ENV_FILE="${INFRA2_HOST_GUARD_ENV:-/etc/infra2/host-guard.env}"

for script in disk_guardian.sh host_heartbeat.sh; do
  bash -n "${REPO_ROOT}/tools/${script}"
done
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "missing root-only host guard environment file: ${ENV_FILE}" >&2
  exit 1
fi
if [[ "$(stat -c '%a:%u' "${ENV_FILE}")" != "600:0" ]]; then
  echo "${ENV_FILE} must be mode 0600 and owned by root" >&2
  exit 1
fi
set -a
# The file is root-owned and private; systemd reads the same assignment format.
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a
urls=("${HOST_HEARTBEAT_PING_URL:-}" "${DISK_GUARDIAN_WARNING_PING_URL:-}" "${DISK_GUARDIAN_PING_URL:-}")
for url in "${urls[@]}"; do
  if [[ ! "${url}" =~ ^https://hc-ping\.com/[A-Za-z0-9/_-]+$ ]] || [[ "${url}" == *replace-with* ]]; then
    echo "host guard requires three real Healthchecks.io ping URLs" >&2
    exit 1
  fi
done
if [[ "${urls[0]}" == "${urls[1]}" || "${urls[0]}" == "${urls[2]}" || "${urls[1]}" == "${urls[2]}" ]]; then
  echo "host guard ping URLs must be distinct" >&2
  exit 1
fi
if [[ "${MODE}" == "--check" ]]; then
  echo "Host guard scripts and root-only environment file are ready"
  exit 0
fi
if [[ "${EUID}" -ne 0 ]]; then
  echo "--apply requires root" >&2
  exit 1
fi

install -m 0755 "${REPO_ROOT}/tools/disk_guardian.sh" /usr/local/sbin/infra2-disk-guardian.sh
install -m 0755 "${REPO_ROOT}/tools/host_heartbeat.sh" /usr/local/sbin/infra2-host-heartbeat.sh
for unit in infra2-disk-guardian.service infra2-disk-guardian.timer infra2-host-heartbeat.service infra2-host-heartbeat.timer; do
  install -m 0644 "${SCRIPT_DIR}/${unit}" "/etc/systemd/system/${unit}"
done
systemctl daemon-reload
systemctl enable --now infra2-disk-guardian.timer infra2-host-heartbeat.timer
systemctl list-timers --no-pager infra2-disk-guardian.timer infra2-host-heartbeat.timer
