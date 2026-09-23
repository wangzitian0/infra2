#!/usr/bin/env bash
# Validate and install infra2's Docker defaults without restarting the daemon.
set -euo pipefail

case "${1:---check}" in
  --check|--apply) MODE="${1:---check}" ;;
  *) echo "usage: $0 [--check|--apply]" >&2; exit 2 ;;
esac
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${INFRA2_DAEMON_JSON:-/etc/docker/daemon.json}"
CANDIDATE="$(mktemp)"
trap 'rm -f "${CANDIDATE}"' EXIT

python3 - "${TARGET}" "${SCRIPT_DIR}/daemon.json" "${CANDIDATE}" <<'PY'
import json
import pathlib
import sys

target, desired_path, output = map(pathlib.Path, sys.argv[1:])
current = json.loads(target.read_text()) if target.exists() else {}
if not isinstance(current, dict):
    raise SystemExit("existing Docker daemon config must be a JSON object")
desired = json.loads(desired_path.read_text())
if current.get("log-driver", "json-file") != "json-file":
    raise SystemExit("existing Docker log driver is not json-file; manual review required")
opts = current.get("log-opts", {})
if not isinstance(opts, dict):
    raise SystemExit("existing Docker log-opts must be a JSON object")
current["log-opts"] = {**opts, **desired["log-opts"]}
current["log-driver"] = desired["log-driver"]
current["live-restore"] = desired["live-restore"]
output.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
PY

dockerd --validate --config-file "${CANDIDATE}"
if [[ "${MODE}" == "--check" ]]; then
  echo "Docker daemon candidate validated; no host settings changed"
  exit 0
fi

if [[ "${EUID}" -ne 0 ]]; then
  echo "--apply requires root" >&2
  exit 1
fi
BACKUP=""
if [[ -f "${TARGET}" ]]; then
  BACKUP="${TARGET}.infra2-backup-$(date -u +%Y%m%dT%H%M%SZ)"
  cp -p "${TARGET}" "${BACKUP}"
fi
install -m 0644 "${CANDIDATE}" "${TARGET}"
if ! systemctl reload docker || [[ "$(docker info --format '{{.LiveRestoreEnabled}}')" != "true" ]]; then
  echo "Docker reload or live-restore verification failed; restoring prior config" >&2
  if [[ -n "${BACKUP}" ]]; then
    cp -p "${BACKUP}" "${TARGET}"
  else
    rm -f "${TARGET}"
  fi
  systemctl reload docker || true
  exit 1
fi
echo "Docker daemon reloaded; live-restore is enabled. New-container log limits require a separate creation probe."
