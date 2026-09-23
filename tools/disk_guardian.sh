#!/usr/bin/env bash
# Fast host-disk guard. The six-hour Dokploy hygiene schedule remains the
# general GC owner; this timer only reacts when the shared filesystem is full.
set -euo pipefail

DATA_PATH="${DISK_GUARDIAN_DATA_PATH:-/data}"
LOG_ROOT="${DISK_GUARDIAN_LOG_ROOT:-}"
WARNING_PERCENT=80
CRITICAL_PERCENT=85
LOG_LIMIT_BYTES=$((100 * 1024 * 1024))
DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then DRY_RUN=true; elif [[ $# -gt 0 ]]; then
  echo "usage: $0 [--dry-run]" >&2
  exit 2
fi

disk_percent() {
  local percent
  percent="$(df -P "${DATA_PATH}" | awk 'NR == 2 { gsub(/%/, "", $5); print $5 }')"
  if [[ ! "${percent}" =~ ^[0-9]+$ ]] || (( percent > 100 )); then
    echo "cannot read disk percentage for ${DATA_PATH}" >&2
    return 1
  fi
  printf '%s\n' "${percent}"
}

if [[ -z "${LOG_ROOT}" ]]; then
  docker_root="$(timeout 5 docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
  LOG_ROOT="${docker_root:-/var/lib/docker}/containers"
fi

ping_check() {
  local variable="$1" suffix="${2:-}" url
  if [[ "${DRY_RUN}" == true ]]; then return 0; fi
  url="${!variable:-}"
  if [[ ! "${url}" =~ ^https://hc-ping\.com/[A-Za-z0-9/_-]+$ ]]; then
    echo "${variable} is missing or invalid" >&2
    return 1
  fi
  if ! curl --fail --silent --connect-timeout 3 --max-time 8 \
    --output /dev/null "${url}${suffix}" 2>/dev/null; then
    echo "${variable} ping failed" >&2
    return 1
  fi
}

before="$(disk_percent)"
echo "disk_guardian: ${DATA_PATH} is ${before}%"
if (( before < WARNING_PERCENT )); then
  ping_check DISK_GUARDIAN_WARNING_PING_URL
  ping_check DISK_GUARDIAN_PING_URL
  exit 0
fi

echo "disk_guardian: warning threshold ${WARNING_PERCENT}% reached; pruning only dangling images and aged build cache"
cleanup_failed=false
if [[ "${DRY_RUN}" == true ]]; then
  echo "[dry-run] docker image prune -f --filter dangling=true"
  echo "[dry-run] docker builder prune -f --filter until=24h"
else
  if ! docker image prune -f --filter dangling=true; then cleanup_failed=true; fi
  if ! docker builder prune -f --filter until=24h; then cleanup_failed=true; fi
fi
if [[ "${cleanup_failed}" == true ]]; then
  echo "disk_guardian: safe cleanup failed" >&2
fi

warning_ping_failed=false
if ! ping_check DISK_GUARDIAN_WARNING_PING_URL /fail; then
  warning_ping_failed=true
fi

after_prune="$(disk_percent)"
if (( before >= CRITICAL_PERCENT || after_prune >= CRITICAL_PERCENT )); then
  echo "disk_guardian: critical threshold ${CRITICAL_PERCENT}% reached"
  if [[ -d "${LOG_ROOT}" ]]; then
    while IFS= read -r -d '' log_path; do
      if ! size_bytes="$(stat -c%s "${log_path}")"; then
        echo "disk_guardian: log vanished or is unreadable: ${log_path}" >&2
        continue
      fi
      if (( size_bytes > LOG_LIMIT_BYTES )); then
        echo "disk_guardian: oversized Docker json log ${log_path} (${size_bytes} bytes)"
        if [[ "${DRY_RUN}" == true ]]; then
          echo "[dry-run] truncate ${log_path}"
        else
          if ! : > "${log_path}"; then
            echo "disk_guardian: cannot truncate ${log_path}" >&2
          fi
        fi
      fi
    done < <(find "${LOG_ROOT}" -type f -name '*-json.log' -print0)
  fi
  ping_check DISK_GUARDIAN_PING_URL /fail
  after="$(disk_percent)"
  echo "disk_guardian: after cleanup ${after}%"
  if (( after >= CRITICAL_PERCENT )) || [[ "${cleanup_failed}" == true ]]; then exit 1; fi
  [[ "${warning_ping_failed}" == false ]]
  exit
fi

after="$(disk_percent)"
echo "disk_guardian: after cleanup ${after}%"
if [[ "${cleanup_failed}" == true ]]; then
  ping_check DISK_GUARDIAN_PING_URL /fail
  exit 1
fi
ping_check DISK_GUARDIAN_PING_URL
[[ "${warning_ping_failed}" == false ]]
