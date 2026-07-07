#!/usr/bin/env bash
# On-host backup runner for infra2 stateful services.
#
# Produces restorable, logical backups (not raw live-datadir tars):
#   - Postgres services: `pg_dumpall` via `docker exec` (crash-consistent logical dump)
#   - Redis services:    `redis-cli SAVE` then archive the resulting dump.rdb
#   - Other services:    gzip tar of the registered data_path
#
# Writes a manifest compatible with tools/backup_verification.py and, when a
# BACKUP_REMOTE rclone target is configured, uploads each archive off-host.
#
# Usage (on the VPS, where /data and the docker socket live):
#   tools/host_backup.sh                      # archive locally to /data/backups/infra2
#   BACKUP_REMOTE=r2:infra2 tools/host_backup.sh   # archive + upload off-host
#
# Environment:
#   BACKUP_OUTPUT_DIR  default /data/backups/infra2
#   BACKUP_REMOTE      optional rclone remote prefix (e.g. r2:infra2); off-host upload
#   ENV_SUFFIX         optional container suffix (e.g. -staging); default production ("")
#   PG_SUPERUSER       postgres superuser for pg_dumpall (default: postgres)
set -euo pipefail

OUTPUT_DIR="${BACKUP_OUTPUT_DIR:-/data/backups/infra2}"
REMOTE="${BACKUP_REMOTE:-}"
SUFFIX="${ENV_SUFFIX:-}"
PG_SUPERUSER="${PG_SUPERUSER:-postgres}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="${OUTPUT_DIR}/${TS}"
MANIFEST="${RUN_DIR}/manifest.json"

mkdir -p "${RUN_DIR}"

# service_id | kind | source (container or data_path)
# Mirrors docs/ssot/ops.backup-inventory.yaml. kind in: pg | redis | path
SERVICES=$(cat <<EOF
bootstrap/vault|path|/data/bootstrap/vault
platform/postgres|pg|platform-postgres${SUFFIX}
platform/redis|redis|platform-redis${SUFFIX}|/data/platform/redis${SUFFIX}
platform/clickhouse|path|/data/platform/clickhouse${SUFFIX}
platform/minio|path|/data/platform/minio${SUFFIX}
platform/authentik|path|/data/platform/authentik${SUFFIX}
finance_report/postgres|pg|finance_report-postgres${SUFFIX}
finance_report/redis|redis|finance_report-redis${SUFFIX}|/data/finance_report/redis${SUFFIX}
truealpha/postgres|pg|truealpha-postgres${SUFFIX}
EOF
)

artifacts=()

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
  artifacts+=("{\"service_id\":\"${service_id}\",\"created_at\":$(date -u +%s),\"size_bytes\":${size},\"sha256\":\"${sha}\",\"remote_uri\":\"${remote_uri}\",\"method\":\"${method}\"}")
}

while IFS= read -r line; do
  [ -z "${line}" ] && continue
  service_id="${line%%|*}"; rest="${line#*|}"
  kind="${rest%%|*}"; rest="${rest#*|}"
  safe_id="${service_id//\//_}"
  case "${kind}" in
    pg)
      container="${rest%%|*}"
      archive="${RUN_DIR}/${safe_id}_${TS}.sql.gz"
      echo "pg_dumpall ${container} -> ${archive}"
      docker exec "${container}" pg_dumpall -U "${PG_SUPERUSER}" | gzip > "${archive}"
      emit_artifact "${service_id}" "${archive}" "pg_dumpall_gz"
      ;;
    redis)
      container="${rest%%|*}"; data_path="${rest#*|}"
      echo "redis SAVE ${container}"
      docker exec "${container}" redis-cli SAVE >/dev/null || true
      archive="${RUN_DIR}/${safe_id}_${TS}.tar.gz"
      tar -czf "${archive}" -C "${data_path}" .
      emit_artifact "${service_id}" "${archive}" "redis_rdb_archive"
      ;;
    path)
      data_path="${rest%%|*}"
      if [ ! -d "${data_path}" ]; then echo "skip missing ${service_id} ${data_path}" >&2; continue; fi
      archive="${RUN_DIR}/${safe_id}_${TS}.tar.gz"
      tar -czf "${archive}" -C "${data_path}" .
      emit_artifact "${service_id}" "${archive}" "filesystem_archive"
      ;;
    *) echo "unknown kind ${kind} for ${service_id}" >&2; exit 2;;
  esac
done <<< "${SERVICES}"

{
  echo "{"
  echo "  \"schema_version\": 1,"
  echo "  \"generated_at\": $(date -u +%s),"
  echo "  \"verified_at\": $(date -u +%s),"
  echo "  \"artifacts\": [$(IFS=,; echo "${artifacts[*]}")]"
  echo "}"
} > "${MANIFEST}"

if [ -n "${REMOTE}" ]; then
  rclone copyto "${MANIFEST}" "${REMOTE%/}/manifest.json"
fi

# Retention: keep the most recent BACKUP_KEEP local run directories.
BACKUP_KEEP="${BACKUP_KEEP:-7}"
ls -1dt "${OUTPUT_DIR}"/*/ 2>/dev/null | tail -n +$((BACKUP_KEEP + 1)) | while read -r old; do
  rm -rf "${old}"
done

echo "${MANIFEST}"
