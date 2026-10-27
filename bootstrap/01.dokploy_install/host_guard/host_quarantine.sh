#!/usr/bin/env bash
# Quarantine VPS host litter with a 15-day retention manifest and retire stale cron jobs.
set -euo pipefail

case "${1:---check}" in
  --check|--stage|--list|--purge-expired) MODE="${1:---check}" ;;
  *) echo "usage: $0 [--check|--stage|--list|--purge-expired]" >&2; exit 2 ;;
esac

INFRA2_QUARANTINE_ROOT_PREFIX="${INFRA2_QUARANTINE_ROOT_PREFIX:-}"
INFRA2_QUARANTINE_DIR="${INFRA2_QUARANTINE_DIR:-${INFRA2_QUARANTINE_ROOT_PREFIX}/data/backups/quarantine}"
INFRA2_QUARANTINE_TTL_DAYS="${INFRA2_QUARANTINE_TTL_DAYS:-15}"
INFRA2_QUARANTINE_BATCH_ID="${INFRA2_QUARANTINE_BATCH_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
INFRA2_QUARANTINE_NOW_EPOCH="${INFRA2_QUARANTINE_NOW_EPOCH:-$(date -u +%s)}"
INFRA2_CRONTAB_FILE="${INFRA2_CRONTAB_FILE:-}"

TARGET_LITTER=(
  "root/ + .Destination)|oct19_ssh_inspect_escape"
  "root/ .Destination}}{{end}}|oct19_ssh_inspect_escape"
  "root/None|unexpanded_deployer_data_path"
  "root/None-staging|unexpanded_deployer_data_path"
  "root/openclaw-discord-data-final-20260915T104201Z.tgz|retired_openclaw_archive"
  "root/backups|legacy_manual_and_burnin_backups"
  "root/openclaw-hotfix-backups|retired_openclaw_archive"
  "root/openclaw-shadow-discussions-removed|retired_openclaw_archive"
  "root/openclaw-volume-backups|retired_openclaw_archive"
  "root/openclaw-appledouble-cleanup-20260526T063744Z.txt|retired_openclaw_archive"
  "root/tianclaw-artifact-delete-20260514.txt|retired_tianclaw_log"
  "root/tianclaw-artifact-delete-vps-only-20260514.txt|retired_tianclaw_log"
  "root/finance_report-prod-pre-v0.1.51-20260916T091608Z.dump|legacy_pre_schema_gate_dump"
  "root/prod_backup.dump|legacy_manual_dump"
  "root/truealpha-postgres-prod.retired-20260910|legacy_pre_iac_truealpha_dir"
  "root/.trash-0917|legacy_manual_trash"
  "root/live_check.out|legacy_debug_output"
  "root/live_cmp.out|legacy_debug_output"
  "root/clickhouse-compose.yaml|legacy_manual_compose"
  "root/staging-signoz.yaml|legacy_manual_compose"
  "root/create_perfect_sso.sh|legacy_manual_script"
  "root/infra2-hostfw|legacy_hostfw_staging_copy"
  "root/zitian|legacy_root_checkout"
  "zookeeper|unexpanded_data_path_mount"
  "certs|unexpanded_data_path_mount"
  "config.yml|unexpanded_data_path_mount"
  "custom-templates|unexpanded_data_path_mount"
  "scripts|unexpanded_data_path_mount"
  "user_scripts|unexpanded_data_path_mount"
  "tmp/infra022-restore-source-a6f29e5|stale_tmp_rehearsal_checkout"
  "tmp/model-recon|stale_tmp_recon"
  "tmp/standards-recon|stale_tmp_recon"
  "tmp/p2|stale_tmp_recon"
  "tmp/probe|stale_tmp_recon"
  "tmp/pgtest.hehE.sh|stale_tmp_debug_script"
  "tmp/yahoo.py|stale_tmp_debug_script"
  "data/backups/manual|legacy_manual_backup_dir"
  "data/finance|retired_wealthfolio_data_dir"
  "data/staging|legacy_staging_data_dir"
  "usr/local/sbin/tianclaw-vps-disk-maintenance.sh|retired_tianclaw_maintenance_script"
  "usr/local/sbin/infra2-host-backup.sh.before-a6f29e5|legacy_backup_script_copy"
  "data/truealpha/ingest|legacy_host_ingest_venv"
  "data/truealpha/issue68|legacy_issue68_venv"
  "data/truealpha/issue68-runtime|legacy_issue68_runtime"
  "data/truealpha/backups|legacy_truealpha_local_backups"
  "data/platform/openpanel-staging|prod_only_service_staging_leftover"
  "data/platform/clickhouse-staging|prod_only_service_staging_leftover"
  "data/platform/signoz-staging|prod_only_service_staging_leftover"
  "data/platform/activepieces|retired_activepieces_data_dir"
  "data/finance_report/app-staging|legacy_empty_app_staging_dir"
  "data/bootstrap/1password_dbreset_backup_20260609T132132Z|legacy_1password_upgrade_backup"
  "data/bootstrap/1password.backup.20260605123024|legacy_1password_upgrade_backup"
  "data/bootstrap/1password.backup.20260605T221434Z|legacy_1password_upgrade_backup"
  "data/bootstrap/1password.cache-before-182.20260605171304|legacy_1password_upgrade_backup"
)

read_crontab() {
  if [[ -n "${INFRA2_CRONTAB_FILE}" ]]; then
    if [[ -f "${INFRA2_CRONTAB_FILE}" ]]; then
      cat "${INFRA2_CRONTAB_FILE}"
    fi
  else
    crontab -l 2>/dev/null || true
  fi
}

write_crontab() {
  local content_file="$1"
  if [[ -n "${INFRA2_CRONTAB_FILE}" ]]; then
    cp "${content_file}" "${INFRA2_CRONTAB_FILE}"
  else
    crontab "${content_file}"
  fi
}

compute_size_and_sha() {
  python3 - "$1" <<'PY'
import hashlib
import os
import stat
import sys

p = sys.argv[1]
st = os.lstat(p)
if stat.S_ISDIR(st.st_mode):
    total = 0
    for root, _, files in os.walk(p, followlinks=False):
        for name in files:
            fp = os.path.join(root, name)
            try:
                total += os.lstat(fp).st_size
            except OSError:
                pass
    print(f"{total} directory")
elif stat.S_ISREG(st.st_mode):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    print(f"{st.st_size} {h.hexdigest()}")
else:
    print(f"{st.st_size} symlink_or_special")
PY
}

check_f3cnn_drift() {
  python3 - "$1" <<'PY'
import os
import sys

d = sys.argv[1]
if not os.path.isdir(d):
    sys.exit(1)
for root, _, files in os.walk(d, followlinks=False):
    for f in files:
        fp = os.path.join(root, f)
        try:
            mode = os.lstat(fp).st_mode & 0o777
            if mode & 0o077:
                sys.exit(0)
        except OSError:
            pass
sys.exit(1)
PY
}

tighten_f3cnn() {
  python3 - "$1" <<'PY'
import os
import stat
import sys

d = sys.argv[1]
if not os.path.isdir(d):
    sys.exit(0)
os.chmod(d, 0o700)
for root, dirs, files in os.walk(d, followlinks=False):
    for sub in dirs:
        dp = os.path.join(root, sub)
        if not os.path.islink(dp):
            os.chmod(dp, 0o700)
    for f in files:
        fp = os.path.join(root, f)
        st = os.lstat(fp)
        if stat.S_ISREG(st.st_mode):
            os.chmod(fp, 0o600)
PY
}

if [[ "${MODE}" == "--check" ]]; then
  for item in "${TARGET_LITTER[@]}"; do
    rel_path="${item%%|*}"
    reason="${item#*|}"
    src="${INFRA2_QUARANTINE_ROOT_PREFIX}/${rel_path}"
    if [[ -e "${src}" || -L "${src}" ]]; then
      read -r size_bytes _ <<< "$(compute_size_and_sha "${src}")"
      echo "FOUND ${size_bytes} /${rel_path} (${reason})"
    fi
  done

  f3cnn_dir="${INFRA2_QUARANTINE_ROOT_PREFIX}/tmp/F3CNN"
  if check_f3cnn_drift "${f3cnn_dir}"; then
    echo "PERMISSION_DRIFT /tmp/F3CNN (files wider than 0600; live OpenD lock dir, do not move)"
  fi

  cron_content="$(read_crontab)"
  if [[ -n "${cron_content}" ]]; then
    while IFS= read -r line || [[ -n "${line}" ]]; do
      if [[ "${line}" =~ ^[[:space:]]*# ]]; then
        continue
      fi
      if [[ "${line}" == *"/root/backups/truealpha-prod-backup.sh"* || "${line}" == *"tianclaw-vps-disk-maintenance.sh"* ]]; then
        echo "STALE_CRON ${line}"
      fi
    done <<< "${cron_content}"
  fi
  exit 0
fi

if [[ "${MODE}" == "--list" ]]; then
  manifest="${INFRA2_QUARANTINE_DIR}/manifest.jsonl"
  if [[ -f "${manifest}" ]]; then
    cat "${manifest}"
  else
    echo "No quarantine manifest found"
  fi
  exit 0
fi

if [[ "${MODE}" == "--stage" ]]; then
  if [[ -z "${INFRA2_QUARANTINE_ROOT_PREFIX}" && "${EUID}" -ne 0 ]]; then
    echo "--stage requires root" >&2
    exit 1
  fi

  now_epoch="${INFRA2_QUARANTINE_NOW_EPOCH}"
  purge_after_epoch=$(( now_epoch + INFRA2_QUARANTINE_TTL_DAYS * 86400 ))
  quarantined_at="$(python3 -c 'import datetime,sys; print(datetime.datetime.fromtimestamp(int(sys.argv[1]), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))' "${now_epoch}")"
  purge_after="$(python3 -c 'import datetime,sys; print(datetime.datetime.fromtimestamp(int(sys.argv[1]), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))' "${purge_after_epoch}")"

  batch_dir="${INFRA2_QUARANTINE_DIR}/${INFRA2_QUARANTINE_BATCH_ID}"
  mkdir -p "${INFRA2_QUARANTINE_DIR}"
  chmod 0700 "${INFRA2_QUARANTINE_DIR}"
  mkdir -p "${batch_dir}/files"
  chmod 0700 "${batch_dir}" "${batch_dir}/files"

  cron_updated=0
  cron_content="$(read_crontab)"
  if [[ -n "${cron_content}" ]]; then
    new_cron_file="$(mktemp)"
    while IFS= read -r line || [[ -n "${line}" ]]; do
      if [[ ! "${line}" =~ ^[[:space:]]*# ]] && [[ "${line}" == *"/root/backups/truealpha-prod-backup.sh"* || "${line}" == *"tianclaw-vps-disk-maintenance.sh"* ]]; then
        printf '# [quarantined %s batch=%s purge_after=%s] %s\n' "${quarantined_at}" "${INFRA2_QUARANTINE_BATCH_ID}" "${purge_after}" "${line}" >> "${new_cron_file}"
        cron_updated=1
      else
        printf '%s\n' "${line}" >> "${new_cron_file}"
      fi
    done <<< "${cron_content}"

    if [[ "${cron_updated}" -eq 1 ]]; then
      printf '%s\n' "${cron_content}" > "${batch_dir}/crontab.before"
      chmod 0600 "${batch_dir}/crontab.before"
      write_crontab "${new_cron_file}"
    fi
    rm -f "${new_cron_file}"
  fi

  moved_count=0
  for item in "${TARGET_LITTER[@]}"; do
    rel_path="${item%%|*}"
    reason="${item#*|}"
    src="${INFRA2_QUARANTINE_ROOT_PREFIX}/${rel_path}"
    if [[ -e "${src}" || -L "${src}" ]]; then
      if [[ "${moved_count}" -eq 0 && "${cron_updated}" -eq 0 && -n "${cron_content}" ]]; then
        printf '%s\n' "${cron_content}" > "${batch_dir}/crontab.before"
        chmod 0600 "${batch_dir}/crontab.before"
      fi
      moved_count=$(( moved_count + 1 ))
      read -r size_bytes sha256 <<< "$(compute_size_and_sha "${src}")"
      dest="${batch_dir}/files/${rel_path}"
      mkdir -p "$(dirname "${dest}")"
      mv -- "${src}" "${dest}"
      python3 - "${INFRA2_QUARANTINE_DIR}/manifest.jsonl" "${batch_dir}/manifest.jsonl" \
        "${INFRA2_QUARANTINE_BATCH_ID}" "/${rel_path}" "${dest}" "${reason}" \
        "${size_bytes}" "${sha256}" "${quarantined_at}" "${now_epoch}" \
        "${purge_after}" "${purge_after_epoch}" <<'PY'
import json
import os
import sys

(
    root_manifest,
    batch_manifest,
    batch_id,
    original_path,
    quarantine_path,
    reason,
    size_bytes,
    sha256,
    quarantined_at,
    quarantined_at_epoch,
    purge_after,
    purge_after_epoch,
) = sys.argv[1:]

record = {
    "batch_id": batch_id,
    "original_path": original_path,
    "quarantine_path": quarantine_path,
    "reason": reason,
    "size_bytes": int(size_bytes),
    "sha256": sha256,
    "quarantined_at": quarantined_at,
    "quarantined_at_epoch": int(quarantined_at_epoch),
    "purge_after": purge_after,
    "purge_after_epoch": int(purge_after_epoch),
    "status": "quarantined",
}
line = json.dumps(record, sort_keys=True) + "\n"
for path in (root_manifest, batch_manifest):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
    os.chmod(path, 0o600)
PY
    fi
  done

  if [[ "${moved_count}" -eq 0 && "${cron_updated}" -eq 0 ]]; then
    rmdir "${batch_dir}/files" "${batch_dir}" 2>/dev/null || true
  fi

  f3cnn_dir="${INFRA2_QUARANTINE_ROOT_PREFIX}/tmp/F3CNN"
  tighten_f3cnn "${f3cnn_dir}"
  exit 0
fi

if [[ "${MODE}" == "--purge-expired" ]]; then
  if [[ -z "${INFRA2_QUARANTINE_ROOT_PREFIX}" && "${EUID}" -ne 0 ]]; then
    echo "--purge-expired requires root" >&2
    exit 1
  fi

  manifest="${INFRA2_QUARANTINE_DIR}/manifest.jsonl"
  if [[ ! -f "${manifest}" ]]; then
    exit 0
  fi

  python3 - "${INFRA2_QUARANTINE_DIR}" "${manifest}" "${INFRA2_QUARANTINE_NOW_EPOCH}" <<'PY'
import json
import os
import pathlib
import shutil
import sys

quarantine_dir = pathlib.Path(sys.argv[1]).resolve()
manifest_path = pathlib.Path(sys.argv[2])
now_epoch = int(sys.argv[3])

lines = manifest_path.read_text(encoding="utf-8").splitlines()
records = [json.loads(line) for line in lines if line.strip()]

batches = {}
for rec in records:
    bid = rec["batch_id"]
    batches.setdefault(bid, []).append(rec)

purged_batches = set()
for bid, recs in batches.items():
    if all(r.get("status") == "quarantined" and int(r["purge_after_epoch"]) <= now_epoch for r in recs):
        batch_dir = (quarantine_dir / bid).resolve()
        if batch_dir.parent != quarantine_dir:
            raise SystemExit(f"refusing to purge path outside quarantine dir: {batch_dir}")
        if batch_dir.exists():
            shutil.rmtree(batch_dir)
        purged_batches.add(bid)

if purged_batches:
    for rec in records:
        if rec["batch_id"] in purged_batches:
            rec["status"] = "purged"
            rec["purged_at_epoch"] = now_epoch
    tmp_path = manifest_path.with_suffix(".jsonl.tmp")
    tmp_path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in records),
        encoding="utf-8",
    )
    os.chmod(tmp_path, 0o600)
    tmp_path.replace(manifest_path)
PY
  exit 0
fi
