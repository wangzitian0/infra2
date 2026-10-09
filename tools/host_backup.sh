#!/usr/bin/env bash
# On-host backup runner for infra2 stateful services.
#
# Produces restorable, logical backups (not raw live-datadir tars):
#   - Postgres services: `pg_dumpall` via `docker exec` (crash-consistent logical dump)
#   - Redis services:    authenticated `redis-cli SAVE`, then archive dump.rdb only
#   - Other services:    gzip tar of the registered data_path
#
# Writes a manifest compatible with tools/backup_verification.py and, when a
# BACKUP_REMOTE rclone target is configured, uploads each archive off-host.
#
# One failing service never stops the others: every service is attempted, the
# manifest lists the ones that succeeded, and the run exits 1 naming each
# failure (the verifier then reports the missing service). Local retention is
# skipped on a failed run, so older good runs are not rotated away.
#
# Usage (on the VPS, where /data and the docker socket live):
#   BACKUP_REMOTE=gdrive-backup:infra2 tools/host_backup.sh           # archive + upload off-host (weekly)
#   BACKUP_REMOTE=gdrive-backup:infra2 BACKUP_TIER=quarterly tools/host_backup.sh # quarterly snapshot
#   BACKUP_LOCAL_ONLY=1 tools/host_backup.sh                          # local archives only (manual test)
#
# A run must name its target (#1030). An unset BACKUP_REMOTE exits 2 before any work:
# on 2026-10-05 a manual run without it replaced the off-host manifest with local
# URIs, and the watchdog paged on all 17 artifacts. A BACKUP_LOCAL_ONLY=1 run writes
# only its own run directory: it never replaces <env>-manifest.json (the off-host
# record the watchdog reads) and deletes no earlier run.
#
# Environment:
#   BACKUP_OUTPUT_DIR  default /data/backups/infra2
#   BACKUP_REMOTE      rclone remote prefix (e.g. gdrive-backup:infra2); required unless BACKUP_LOCAL_ONLY=1
#   BACKUP_LOCAL_ONLY  1 = archive locally only; mutually exclusive with BACKUP_REMOTE
#   BACKUP_TIER        optional retention tier: weekly (default) | quarterly
#   BACKUP_DATA_ROOT   host data root holding the service data paths (default /data)
#   BACKUP_KEEP        local run directories to keep per environment (default 7)
#   ENV_SUFFIX         optional container suffix (e.g. -staging); default production ("")
#   PG_SUPERUSER       postgres superuser for pg_dumpall (default: postgres)
set -euo pipefail
umask 077  # Local archives include Vault and 1Password Connect state.

OUTPUT_DIR="${BACKUP_OUTPUT_DIR:-/data/backups/infra2}"
REMOTE="${BACKUP_REMOTE:-}"
LOCAL_ONLY="${BACKUP_LOCAL_ONLY:-}"
case "${LOCAL_ONLY}" in
  ""|0|1) ;;
  *) echo "BACKUP_LOCAL_ONLY must be 1, 0 or unset (got '${LOCAL_ONLY}')" >&2; exit 2 ;;
esac
if [ -n "${REMOTE}" ] && [ "${LOCAL_ONLY}" = "1" ]; then
  echo "BACKUP_REMOTE and BACKUP_LOCAL_ONLY are mutually exclusive" >&2
  exit 2
fi
if [ -z "${REMOTE}" ] && [ "${LOCAL_ONLY}" != "1" ]; then
  echo "BACKUP_REMOTE is not set: set it to the off-host rclone target, or set BACKUP_LOCAL_ONLY=1 for a local-only run that never replaces the off-host manifest (#1030)" >&2
  exit 2
fi
TIER="${BACKUP_TIER:-weekly}"
if [ "${TIER}" != "weekly" ] && [ "${TIER}" != "quarterly" ]; then
  echo "invalid BACKUP_TIER '${TIER}': must be 'weekly' or 'quarterly'" >&2
  exit 2
fi
DATA_ROOT="${BACKUP_DATA_ROOT:-/data}"
SUFFIX="${ENV_SUFFIX:-}"
case "${SUFFIX}" in
  "") ENVIRONMENT="production" ;;
  "-staging") ENVIRONMENT="staging" ;;
  *) echo "unsupported ENV_SUFFIX '${SUFFIX}': expected empty or -staging" >&2; exit 2 ;;
esac
PG_SUPERUSER="${PG_SUPERUSER:-postgres}"
BACKUP_KEEP="${BACKUP_KEEP:-7}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_ID="${TS}-$$"
RUN_DIR="${OUTPUT_DIR}/${ENVIRONMENT}-${RUN_ID}"
MANIFEST="${RUN_DIR}/manifest.json"
LATEST_MANIFEST="${OUTPUT_DIR}/${ENVIRONMENT}-manifest.json"
ARTIFACTS_FILE="${RUN_DIR}/.artifacts.jsonl"

RUN_REMOTE=""
if [ -n "${REMOTE}" ]; then
  RUN_REMOTE="${REMOTE%/}/${TIER}/${ENVIRONMENT}/${RUN_ID}"
fi

mkdir -p "${RUN_DIR}"
chmod 700 "${OUTPUT_DIR}" "${RUN_DIR}"
: > "${ARTIFACTS_FILE}"

# service_id | kind | source (container or data_path)
# Derived dynamically from declared BackupFacet inventory (libs.backup.emitter).
# Preserves safety invariant #618: logical dumps FIRST, busy path archives LAST.
# Manual override via SERVICES is preserved for testing or single-service runs.
if [ -z "${SERVICES:-}" ]; then
  # The host installs this script as a symlink (/usr/local/sbin/infra2-host-backup.sh
  # -> /opt/infra2/tools/host_backup.sh). Resolve the link first, or the repo root
  # is /usr/local and the emitter import fails (#1030).
  REAL_PATH="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || python3 -c "import os, sys; print(os.path.realpath(sys.argv[1]))" "${BASH_SOURCE[0]}")"
  SCRIPT_DIR="$(cd "$(dirname "${REAL_PATH}")" && pwd)"
  REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
  SERVICES="$(PYTHONPATH="${REPO_ROOT}:${PYTHONPATH:-}" python3 -m libs.backup.emitter --environment "${ENVIRONMENT}" --data-root "${DATA_ROOT}")"
fi
if [ -z "${SERVICES//[[:space:]]/}" ]; then
  # An empty list would write a manifest with 0 artifacts and exit 0.
  echo "host_backup: no services to back up (libs.backup.emitter printed nothing)" >&2
  rm -f "${ARTIFACTS_FILE}"
  rmdir "${RUN_DIR}" 2>/dev/null || true
  exit 1
fi

sha256_of() { sha256sum "$1" | awk '{print $1}'; }

emit_artifact() {
  local service_id="$1" archive="$2" method="$3"
  local size sha remote_uri
  size=$(stat -c%s "${archive}")
  sha=$(sha256_of "${archive}")
  remote_uri="local:${archive}"
  if [ -n "${RUN_REMOTE}" ]; then
    remote_uri="${RUN_REMOTE}/${service_id}/$(basename "${archive}")"
    rclone copyto "${archive}" "${remote_uri}"
  elif [ -n "${REMOTE}" ]; then
    remote_uri="${REMOTE%/}/${service_id}/$(basename "${archive}")"
    rclone copyto "${archive}" "${remote_uri}"
  fi
  echo "{\"service_id\":\"${service_id}\",\"created_at\":$(date -u +%s),\"size_bytes\":${size},\"sha256\":\"${sha}\",\"remote_uri\":\"${remote_uri}\",\"method\":\"${method}\"}" >> "${ARTIFACTS_FILE}"
}

# archive_tree <service_id> <archive> <dir> <member>
# tar exit 1 = files changed or vanished mid-read: acceptable for a live path
# (the archive is crash-consistent at best), so it is logged as a WARN and the
# archive kept; exit >=2 is a real failure and fails this service.
archive_tree() {
  local service_id="$1" archive="$2" dir="$3" member="$4" rc=0
  tar --warning=no-file-changed -czf "${archive}" -C "${dir}" "${member}" || rc=$?
  if [ "${rc}" -eq 1 ]; then
    echo "WARN ${service_id}: tar exit 1 (files changed or vanished mid-read); archive kept as crash-consistent" >&2
  elif [ "${rc}" -ne 0 ]; then
    rm -f "${archive}"
    echo "tar failed (${rc}) for ${service_id}" >&2
    return "${rc}"
  fi
}

# backup_service <registry line>; runs under `set -e` in its own subshell.
backup_service() {
  local line="$1" service_id rest kind safe_id container data_path archive reply
  service_id="${line%%|*}"; rest="${line#*|}"
  kind="${rest%%|*}"; rest="${rest#*|}"
  safe_id="${service_id//\//_}"
  case "${kind}" in
    pg)
      container="${rest%%|*}"
      archive="${RUN_DIR}/${safe_id}_${TS}.sql.gz"
      echo "pg_dumpall ${container} -> ${archive}"
      if ! docker exec "${container}" pg_dumpall -U "${PG_SUPERUSER}" | gzip > "${archive}"; then
        rm -f "${archive}"  # a truncated dump must not look restorable
        echo "pg_dumpall failed for ${service_id}" >&2
        return 1
      fi
      emit_artifact "${service_id}" "${archive}" "pg_dumpall_gz"
      ;;
    redis)
      container="${rest%%|*}"; data_path="${rest#*|}"
      echo "redis SAVE ${container}"
      # The servers run --requirepass: a bare `redis-cli SAVE` answers NOAUTH
      # with exit 0 and never snapshots. Authenticate from the container's own
      # rendered secrets; if SAVE is not confirmed, the archive holds the last
      # automatic snapshot (--save 60 1), so warn instead of failing.
      # shellcheck disable=SC2016  # $PASSWORD expands inside the container
      reply=$(docker exec "${container}" sh -c '. /secrets/.env && REDISCLI_AUTH="$PASSWORD" redis-cli SAVE' 2>&1) || true
      if [ "${reply}" != "OK" ]; then
        echo "WARN ${service_id}: SAVE not confirmed; archiving the last automatic snapshot" >&2
      fi
      if [ ! -f "${data_path}/dump.rdb" ]; then
        echo "no dump.rdb for ${service_id} in ${data_path}" >&2
        return 1
      fi
      archive="${RUN_DIR}/${safe_id}_${TS}.tar.gz"
      archive_tree "${service_id}" "${archive}" "${data_path}" dump.rdb
      emit_artifact "${service_id}" "${archive}" "redis_rdb_archive"
      ;;
    path)
      data_path="${rest%%|*}"
      if [ ! -d "${data_path}" ]; then
        echo "missing data directory for ${service_id}: ${data_path}" >&2
        return 1
      fi
      archive="${RUN_DIR}/${safe_id}_${TS}.tar.gz"
      archive_tree "${service_id}" "${archive}" "${data_path}" .
      emit_artifact "${service_id}" "${archive}" "filesystem_archive"
      ;;
    *) echo "unknown kind ${kind} for ${service_id}" >&2; return 2;;
  esac
}

failures=()
while IFS= read -r line; do
  [ -z "${line}" ] && continue
  # A subshell outside any `if`/`||` keeps `set -e` live inside it (bash
  # ignores errexit for functions called in a condition).
  set +e
  ( set -e; backup_service "${line}" ) < /dev/null
  rc=$?
  set -e
  if [ "${rc}" -ne 0 ]; then
    echo "FAILED ${line%%|*} (exit ${rc})" >&2
    failures+=("${line%%|*}")
  fi
done <<< "${SERVICES}"

{
  echo "{"
  echo "  \"schema_version\": 1,"
  echo "  \"environment\": \"${ENVIRONMENT}\","
  echo "  \"generated_at\": $(date -u +%s),"
  echo "  \"verified_at\": $(date -u +%s),"
  echo "  \"artifacts\": [$(paste -sd, "${ARTIFACTS_FILE}")]"
  echo "}"
} > "${MANIFEST}"
rm -f "${ARTIFACTS_FILE}"
if [ -n "${REMOTE}" ]; then
  install -m 0600 "${MANIFEST}" "${LATEST_MANIFEST}"
else
  echo "host_backup: local-only run; ${LATEST_MANIFEST} (the off-host record) is unchanged" >&2
fi

if [ -n "${RUN_REMOTE}" ]; then
  rclone copyto "${MANIFEST}" "${RUN_REMOTE}/manifest.json"
fi
if [ -n "${REMOTE}" ]; then
  rclone copyto "${MANIFEST}" "${REMOTE%/}/${ENVIRONMENT}/manifest.json"
fi

if [ "${#failures[@]}" -gt 0 ]; then
  echo "host_backup: ${#failures[@]} service(s) FAILED: ${failures[*]}; local retention skipped" >&2
  echo "${MANIFEST}"
  exit 1
fi

if [ -z "${REMOTE}" ]; then
  # A local-only run deletes nothing: the runs it would rotate away can be the
  # local copies of off-host runs.
  echo "${MANIFEST}"
  exit 0
fi

# Local Retention: keep BACKUP_KEEP runs per environment. Legacy unsuffixed
# directories are left intact until an operator verifies and retires them.
find "${OUTPUT_DIR}" -maxdepth 1 -type d -name "${ENVIRONMENT}-*" -print | sort -r | tail -n +$((BACKUP_KEEP + 1)) | while IFS= read -r old; do
  rm -rf -- "${old}"
done

# Remote Retention: automated lifecycle policy on off-host backend (Google Drive)
if [ -n "${REMOTE}" ]; then
  if [ "${TIER}" = "weekly" ]; then
    # Weekly backups retained for 60 days (~8 weeks)
    echo "Pruning expired weekly backups older than 60d on ${REMOTE%/}/weekly..."
    rclone delete --min-age 60d "${REMOTE%/}/weekly" 2>/dev/null || true
    rclone rmdirs --leave-root "${REMOTE%/}/weekly" 2>/dev/null || true
  elif [ "${TIER}" = "quarterly" ]; then
    # Quarterly backups retained for 730 days (2 years, total 8 snapshots)
    echo "Pruning expired quarterly backups older than 730d on ${REMOTE%/}/quarterly..."
    rclone delete --min-age 730d "${REMOTE%/}/quarterly" 2>/dev/null || true
    rclone rmdirs --leave-root "${REMOTE%/}/quarterly" 2>/dev/null || true
  fi
fi

echo "${MANIFEST}"
