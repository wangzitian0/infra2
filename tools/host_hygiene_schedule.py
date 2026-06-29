#!/usr/bin/env python3
"""VPS host hygiene — infra2-owned generic Docker/journal/disk GC for the shared host.

Deployment-environment GC belongs to infra2. This provisions (and keeps provisioned)
the Dokploy ``dokploy-server`` Schedule Job that prunes generic host garbage the
platform doesn't otherwise own: aged stopped containers, builder/image/network
caches, journald, oversized Docker json-logs, and disk-usage alerting. The Dokploy
schedule itself is the executor (cron on-host); this tool is the authoritative
provisioner — `--ensure` is idempotent and re-asserts the schedule so it can't drift.

PR preview environments are reaped by infra2's event-driven teardown
(`preview-teardown.yml`) + the `preview-leak-check` fallback; generic host hygiene
NEVER touches them (the `PR_PREVIEW_CONTAINER_PATTERN` below only *excludes* them).
"""

from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import quote

from tools.deploy_env_config import PREVIEW_KINDS

DEFAULT_SCHEDULE_NAME = "finance-report-vps-host-hygiene"
DEFAULT_CRON_EXPRESSION = "17 3,9,15,21 * * *"
DEFAULT_TIMEZONE = "Asia/Singapore"
# Host-level schedules MUST be "dokploy-server" on Dokploy v0.29.x. The legacy
# "server" type (with a null serverId) is accepted by schedule.create but never
# executes the command — that silent no-op is what let host garbage accumulate.
DEFAULT_SCHEDULE_TYPE = "dokploy-server"
# Preview containers are owned by infra2's preview lifecycle; generic hygiene only
# EXCLUDES them, never prunes them. The pattern is derived from the canonical
# sources so it can't silently drift from real container names:
#   - service names: the `container_name`s in finance_report/finance_report/preview/
#     compose.yaml — finance_report-{backend,frontend,preview-db,app-vault-agent}
#   - kind: PREVIEW_KINDS (branch/pr/commit/tag), the alias kinds preview_alias emits
# Full name = finance_report-<service><ENV_SUFFIX>, ENV_SUFFIX = -<kind>-<slug>
# (e.g. finance_report-backend-pr-5, finance_report-preview-db-branch-main).
_PREVIEW_SERVICES = ("backend", "frontend", "preview-db", "app-vault-agent")
PR_PREVIEW_CONTAINER_PATTERN = (
    rf"^finance_report-({'|'.join(_PREVIEW_SERVICES)})"
    rf"-({'|'.join(PREVIEW_KINDS)})-[a-z0-9][a-z0-9-]*$"
)


def build_hygiene_script(
    *,
    dry_run: bool,
    container_prune_until: str,
    builder_prune_until: str,
    image_prune_until: str,
    network_prune_until: str,
    journal_vacuum_time: str,
    journal_vacuum_size: str,
    docker_log_truncate_size_mib: int,
    disk_warning_percent: int,
    disk_error_percent: int,
) -> str:
    lines = [
        "set -eu",
        f"DOCKER_LOG_TRUNCATE_SIZE_MIB='{docker_log_truncate_size_mib}'",
        f"DISK_WARNING_PERCENT='{disk_warning_percent}'",
        f"DISK_ERROR_PERCENT='{disk_error_percent}'",
        f"CONTAINER_PRUNE_UNTIL='{container_prune_until}'",
        f"PR_PREVIEW_CONTAINER_PATTERN='{PR_PREVIEW_CONTAINER_PATTERN}'",
        "docker_usage_summary() {",
        "  if command -v timeout >/dev/null 2>&1; then",
        '    timeout 20 docker system df -v || timeout 20 docker system df || echo "::warning::docker system df unavailable within 20s"',
        "  else",
        '    docker system df -v || docker system df || echo "::warning::docker system df unavailable"',
        "  fi",
        "}",
        "parse_utc_epoch() {",
        '  timestamp="$1"',
        '  if [ -z "$timestamp" ]; then',
        "    return 1",
        "  fi",
        '  date -u -d "$timestamp" +%s 2>/dev/null',
        "}",
        "relative_cutoff_epoch() {",
        '  value="$1"',
        '  now_epoch="$(date -u +%s)"',
        '  amount=""',
        '  case "$value" in',
        '    *h) amount="${value%h}"; multiplier=3600 ;;',
        '    *d) amount="${value%d}"; multiplier=86400 ;;',
        '    *) date -u -d "${value} ago" +%s; return ;;',
        "  esac",
        '  case "$amount" in',
        '    ""|*[!0-9]*) date -u -d "${value} ago" +%s ;;',
        '    *) echo "$((now_epoch - amount * multiplier))" ;;',
        "  esac",
        "}",
        'DISK_PATHS="/"',
        'if [ -d /data ]; then DISK_PATHS="$DISK_PATHS /data"; fi',
        'echo "Disk usage before host hygiene:"',
        "df -h $DISK_PATHS",
        'echo "Docker usage before host hygiene:"',
        "if command -v docker >/dev/null 2>&1; then",
        "  docker_usage_summary",
        "else",
        '  echo "::warning::docker unavailable; skipping Docker usage summary"',
        "fi",
    ]

    lines.extend(
        [
            'echo "Cleaning old non-preview stopped containers"',
            "if command -v docker >/dev/null 2>&1; then",
            '  container_cutoff_epoch="$(relative_cutoff_epoch "$CONTAINER_PRUNE_UNTIL")"',
            "  docker ps -a --format '{{.Names}}' | "
            'grep -Ev "$PR_PREVIEW_CONTAINER_PATTERN" | '
            "while read -r non_preview_container; do",
            '    status="$(docker inspect --format "{{.State.Status}}" "$non_preview_container" 2>/dev/null || echo "")"',
            '    case "$status" in exited|created|dead) ;; *) continue ;; esac',
            '    created_at="$(docker inspect --format "{{.Created}}" "$non_preview_container" 2>/dev/null || echo "")"',
            '    created_epoch="$(parse_utc_epoch "$created_at" || true)"',
            '    if [ -z "$created_epoch" ]; then',
            '      echo "::warning::Skipping deletion because timestamp is missing or unparseable for container ${non_preview_container}"',
            "      continue",
            "    fi",
            '    if [ "$created_epoch" -lt "$container_cutoff_epoch" ]; then',
            (
                '      echo "[dry-run] docker rm -f ${non_preview_container}"'
                if dry_run
                else '      docker rm -f "$non_preview_container" || true'
            ),
            "    fi",
            "  done",
            "else",
            '  echo "::warning::docker unavailable; skipping non-preview container cleanup"',
            "fi",
        ]
    )

    if docker_log_truncate_size_mib > 0:
        lines.extend(
            [
                'echo "Checking Docker json log sizes"',
                "if [ -d /var/lib/docker/containers ]; then",
                "  find /var/lib/docker/containers -name '*-json.log' -type f "
                "| while read -r log_path; do",
                "  size_mib=$(du -m \"$log_path\" | awk '{print $1}')",
                '  if [ "$size_mib" -gt "$DOCKER_LOG_TRUNCATE_SIZE_MIB" ]; then',
                '    echo "Truncating oversized Docker log: ${size_mib}MiB ${log_path}"',
                (
                    '    echo "[dry-run] truncate Docker json log ${log_path}"'
                    if dry_run
                    else '    : > "$log_path"'
                ),
                "  fi",
                "done",
                "else",
                '  echo "::warning::Docker container log directory unavailable; skipping log truncation"',
                "fi",
            ]
        )

    prune_commands = [
        (
            f"docker builder prune -af --filter until={builder_prune_until}",
            f'docker builder prune -af --filter "until={builder_prune_until}"',
        ),
        (
            f"docker image prune -af --filter until={image_prune_until}",
            f'docker image prune -af --filter "until={image_prune_until}"',
        ),
        (
            f"journalctl --vacuum-time={journal_vacuum_time} --vacuum-size={journal_vacuum_size}",
            f'journalctl --vacuum-time="{journal_vacuum_time}" --vacuum-size="{journal_vacuum_size}"',
        ),
    ]
    if network_prune_until == "all":
        prune_commands.insert(2, ("docker network prune -f", "docker network prune -f"))
    else:
        prune_commands.insert(
            2,
            (
                f"docker network prune -f --filter until={network_prune_until}",
                f'docker network prune -f --filter "until={network_prune_until}"',
            ),
        )
    for dry_run_command, command in prune_commands:
        if dry_run:
            lines.append(f'echo "[dry-run] {dry_run_command}"')
        elif command.startswith("journalctl "):
            lines.extend(
                [
                    "if command -v journalctl >/dev/null 2>&1; then",
                    f'  {command} || echo "::warning::journalctl vacuum failed; continuing"',
                    "else",
                    '  echo "::warning::journalctl unavailable; skipping journal vacuum"',
                    "fi",
                ]
            )
        else:
            lines.append(f"{command} || true")

    lines.extend(
        [
            'echo "Disk usage after host hygiene:"',
            "df -h $DISK_PATHS",
            'echo "Docker usage after host hygiene:"',
            "if command -v docker >/dev/null 2>&1; then",
            "  docker_usage_summary",
            "else",
            '  echo "::warning::docker unavailable; skipping Docker usage summary"',
            "fi",
            'df -P $DISK_PATHS | awk -v warn="$DISK_WARNING_PERCENT" '
            '-v err="$DISK_ERROR_PERCENT" \''
            'NR > 1 { usage=$5; gsub(/%/, "", usage); '
            "if (usage + 0 >= err + 0) { "
            'printf "::error::Disk usage for %s is %s%%; critical threshold is %s%%\\n", '
            "$6, usage, err; failed=1 } "
            "else if (usage + 0 >= warn + 0) { "
            'printf "::warning::Disk usage for %s is %s%%; warning threshold is %s%%\\n", '
            "$6, usage, warn } } "
            "END { exit failed ? 1 : 0 }'",
        ]
    )
    return "\n".join(lines) + "\n"


def build_schedule_payload(
    *,
    server_id: str | None,
    script: str,
    name: str = DEFAULT_SCHEDULE_NAME,
    cron_expression: str = DEFAULT_CRON_EXPRESSION,
    timezone: str = DEFAULT_TIMEZONE,
    enabled: bool = True,
    schedule_id: str = "",
) -> dict[str, object]:
    payload_server_id = (
        None if server_id in (None, "null", "undefined", "") else server_id
    )
    payload: dict[str, object] = {
        "name": name,
        "description": (
            "infra2-owned VPS host hygiene. Prunes aged stopped containers, "
            "builder/image/network caches, vacuums the journal, truncates oversized "
            "Docker json logs, and alerts on disk usage. PR preview environments are "
            "reaped by infra2's preview teardown/leak-check and are not touched here."
        ),
        "cronExpression": cron_expression,
        "shellType": "bash",
        "scheduleType": DEFAULT_SCHEDULE_TYPE,
        "command": script,
        "script": script,
        "serverId": payload_server_id,
        "enabled": enabled,
        "timezone": timezone,
    }
    if schedule_id:
        payload["scheduleId"] = schedule_id
    return payload


def extract_schedules(data: object) -> list[dict]:
    """Normalize a schedule.list response (list or wrapped dict) to a list of dicts."""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("schedules", "schedule", "items", "data"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def find_schedule_id_by_name(
    client,
    *,
    server_id: str | None,
    name: str,
    schedule_type: str = DEFAULT_SCHEDULE_TYPE,
) -> str | None:
    query_id = "null" if server_id in (None, "null", "undefined", "") else server_id
    body = client._request(
        "GET",
        "schedule.list?"
        f"id={quote(query_id, safe='')}&scheduleType={quote(schedule_type, safe='')}",
    )
    for schedule in extract_schedules(body):
        if schedule.get("name") == name and schedule.get("scheduleId"):
            return str(schedule["scheduleId"])
    return None


def ensure_host_hygiene_schedule(
    client,
    *,
    server_id: str | None,
    script: str,
    name: str,
    cron_expression: str,
    timezone: str,
    enabled: bool,
) -> str:
    schedule_id = find_schedule_id_by_name(client, server_id=server_id, name=name)
    payload = build_schedule_payload(
        server_id=server_id,
        script=script,
        name=name,
        cron_expression=cron_expression,
        timezone=timezone,
        enabled=enabled,
        schedule_id=schedule_id or "",
    )
    endpoint = "schedule.update" if schedule_id else "schedule.create"
    # Idempotent: re-asserting the same schedule is safe to retry on a churning
    # control plane.
    body = client._request("POST", endpoint, json=payload, idempotent=True)
    effective_schedule_id = str((body or {}).get("scheduleId") or schedule_id or "")
    print(
        f"Dokploy host hygiene schedule {'updated' if schedule_id else 'created'}: "
        f"{effective_schedule_id or name}"
    )
    return effective_schedule_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--container-prune-until", default="72h")
    parser.add_argument("--builder-prune-until", default="72h")
    parser.add_argument("--image-prune-until", default="72h")
    parser.add_argument("--network-prune-until", default="all")
    parser.add_argument("--journal-vacuum-time", default="3d")
    parser.add_argument("--journal-vacuum-size", default="1G")
    parser.add_argument("--docker-log-truncate-size-mib", type=int, default=100)
    parser.add_argument("--disk-warning-percent", type=int, default=85)
    parser.add_argument("--disk-error-percent", type=int, default=95)
    parser.add_argument("--print-dokploy-schedule-payload", action="store_true")
    parser.add_argument(
        "--ensure",
        "--ensure-dokploy-schedule",
        dest="ensure",
        action="store_true",
        help="provision/update the Dokploy host-hygiene schedule (idempotent)",
    )
    parser.add_argument("--server-id", default="null")
    parser.add_argument("--schedule-name", default=DEFAULT_SCHEDULE_NAME)
    parser.add_argument("--cron-expression", default=DEFAULT_CRON_EXPRESSION)
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--disabled", action="store_true")
    parser.add_argument(
        "--emit-script",
        action="store_true",
        help="print the hygiene script to stdout instead of provisioning the schedule",
    )
    args = parser.parse_args(argv)

    script = build_hygiene_script(
        dry_run=args.dry_run,
        container_prune_until=args.container_prune_until,
        builder_prune_until=args.builder_prune_until,
        image_prune_until=args.image_prune_until,
        network_prune_until=args.network_prune_until,
        journal_vacuum_time=args.journal_vacuum_time,
        journal_vacuum_size=args.journal_vacuum_size,
        docker_log_truncate_size_mib=args.docker_log_truncate_size_mib,
        disk_warning_percent=args.disk_warning_percent,
        disk_error_percent=args.disk_error_percent,
    )
    if args.emit_script:
        print(script)
        return 0
    if args.print_dokploy_schedule_payload:
        print(
            json.dumps(
                build_schedule_payload(
                    server_id=args.server_id,
                    script=script,
                    name=args.schedule_name,
                    cron_expression=args.cron_expression,
                    timezone=args.timezone,
                    enabled=not args.disabled,
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.ensure:
        from libs.dokploy import get_dokploy

        ensure_host_hygiene_schedule(
            get_dokploy(),
            server_id=args.server_id,
            script=script,
            name=args.schedule_name,
            cron_expression=args.cron_expression,
            timezone=args.timezone,
            enabled=not args.disabled,
        )
        return 0

    parser.error(
        "nothing to do: pass --ensure, --print-dokploy-schedule-payload, or --emit-script"
    )
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
