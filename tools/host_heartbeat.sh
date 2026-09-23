#!/usr/bin/env bash
# Independent dead-man switch: a stopped host cannot send this heartbeat.
set -euo pipefail

url="${HOST_HEARTBEAT_PING_URL:-}"
if [[ ! "${url}" =~ ^https://hc-ping\.com/[A-Za-z0-9/_-]+$ ]]; then
  echo "HOST_HEARTBEAT_PING_URL is missing or invalid" >&2
  exit 2
fi

suffix=""
if ! timeout 5 docker info > /dev/null 2>&1; then
  suffix="/fail"
fi

if ! curl --fail --silent --connect-timeout 3 --max-time 8 \
  --output /dev/null "${url}${suffix}" 2>/dev/null; then
  echo "host_heartbeat: external ping failed" >&2
  exit 1
fi

if [[ -n "${suffix}" ]]; then
  echo "host_heartbeat: Docker is unavailable; external failure reported" >&2
  exit 1
fi
