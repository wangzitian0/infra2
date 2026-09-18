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
#   tools/host_backup.sh                      # archive locally to /data/backups/infra2
#   BACKUP_REMOTE=r2:infra2 tools/host_backup.sh   # archive + upload off-host
#
# Environment:
#   BACKUP_OUTPUT_DIR  default /data/backups/infra2
#   BACKUP_REMOTE      optional rclone remote prefix (e.g. r2:infra2); off-host upload
#   BACKUP_DATA_ROOT   host data root holding the service data paths (default /data)
#   BACKUP_KEEP        local run directories to keep (default 7)
#   ENV_SUFFIX         optional container suffix (e.g. -staging); default production ("")
#   PG_SUPERUSER       postgres superuser for pg_dumpall (default: postgres)
set -euo pipefail

OUTPUT_DIR="${BACKUP_OUTPUT_DIR:-/data/backups/infra2}"
REMOTE="${BACKUP_REMOTE:-}"
DATA_ROOT="${BACKUP_DATA_ROOT:-/data}"
SUFFIX="${ENV_SUFFIX:-}"
PG_SUPERUSER="${PG_SUPERUSER:-postgres}"
BACKUP_KEEP="${BACKUP_KEEP:-7}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="${OUTPUT_DIR}/${TS}"
MANIFEST="${RUN_DIR}/manifest.json"
ARTIFACTS_FILE="${RUN_DIR}/.artifacts.jsonl"

mkdir -p "${RUN_DIR}"
: > "${ARTIFACTS_FILE}"

# service_id | kind | source (container or data_path)
# Mirrors the BackupFacet declarations on each service's deploy.py
# (libs.backup_verification.load_backup_inventory derives the inventory; the
# handwritten ops.backup-inventory.yaml was deleted in #542). kind in: pg | redis | path
# Logical dumps FIRST (atomic, most critical), busy path archives LAST: the
# 2026-08-20 restore drill (truealpha#650) found every scheduled prod run had
# been ABORTING at platform/minio -- tar exits 1 when live files change under
# it, set -e killed the run, and every service registered after minio
# (finance_report, truealpha) was silently never backed up.
SERVICES=$(cat <<EOF
platform/postgres|pg|platform-postgres${SUFFIX}
finance_report/postgres|pg|finance_report-postgres${SUFFIX}
truealpha/postgres|pg|truealpha-postgres${SUFFIX}
platform/redis|redis|platform-redis${SUFFIX}|${DATA_ROOT}/platform/redis${SUFFIX}
finance_report/redis|redis|finance_report-redis${SUFFIX}|${DATA_ROOT}/finance_report/redis${SUFFIX}
bootstrap/vault|path|${DATA_ROOT}/bootstrap/vault
platform/clickhouse|path|${DATA_ROOT}/platform/clickhouse${SUFFIX}
platform/authentik|path|${DATA_ROOT}/platform/authentik${SUFFIX}
platform/minio|path|${DATA_ROOT}/platform/minio${SUFFIX}
EOF
)

sha256_of() { sha256sum "$1" | awk '{print $1}'; }

emit_artifact() {
  local service_id="$1" archive="$2" method="$3"
  local size sha remote_uri
  size=$(stat -c%s "${archive}")
  sha=$(sha256_of "${archive}")
  remote_uri="local:${archive}"
  if [ -n "${REMOTE}" ]; then
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
  echo "  \"generated_at\": $(date -u +%s),"
  echo "  \"verified_at\": $(date -u +%s),"
  echo "  \"artifacts\": [$(paste -sd, "${ARTIFACTS_FILE}")]"
  echo "}"
} > "${MANIFEST}"
rm -f "${ARTIFACTS_FILE}"

if [ -n "${REMOTE}" ]; then
  rclone copyto "${MANIFEST}" "${REMOTE%/}/manifest.json"
fi

if [ "${#failures[@]}" -gt 0 ]; then
  echo "host_backup: ${#failures[@]} service(s) FAILED: ${failures[*]}; local retention skipped" >&2
  echo "${MANIFEST}"
  exit 1
fi

# Retention: keep the most recent BACKUP_KEEP local run directories.
ls -1dt "${OUTPUT_DIR}"/*/ 2>/dev/null | tail -n +$((BACKUP_KEEP + 1)) | while read -r old; do
  rm -rf "${old}"
done

echo "${MANIFEST}"
