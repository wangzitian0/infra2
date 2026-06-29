"""infra2 owns generic VPS host hygiene (Dokploy dokploy-server schedule provisioner)."""

from __future__ import annotations

import subprocess
import sys

from tools import host_hygiene_schedule as hh


def _script(**overrides) -> str:
    kwargs = dict(
        dry_run=False,
        container_prune_until="72h",
        builder_prune_until="72h",
        image_prune_until="72h",
        network_prune_until="all",
        journal_vacuum_time="3d",
        journal_vacuum_size="1G",
        docker_log_truncate_size_mib=100,
        disk_warning_percent=85,
        disk_error_percent=95,
    )
    kwargs.update(overrides)
    return hh.build_hygiene_script(**kwargs)


def test_script_prunes_generic_host_garbage_and_excludes_previews() -> None:
    script = _script()
    assert "docker builder prune -af" in script
    assert "docker image prune -af" in script
    assert "docker network prune -f" in script
    assert "journalctl --vacuum-time" in script
    assert "Docker json log sizes" in script
    # previews are infra2-owned elsewhere — hygiene only EXCLUDES them, never prunes
    assert hh.PR_PREVIEW_CONTAINER_PATTERN in script
    assert 'grep -Ev "$PR_PREVIEW_CONTAINER_PATTERN"' in script


def test_script_runs_real_commands_when_not_dry_run() -> None:
    real = _script(dry_run=False)
    dry = _script(dry_run=True)
    assert "[dry-run]" not in real
    assert "[dry-run] docker builder prune" in dry


def test_script_is_shell_parseable() -> None:
    proc = subprocess.run(
        ["sh", "-n", "-"], input=_script(), text=True, capture_output=True
    )
    assert proc.returncode == 0, proc.stderr


def test_schedule_payload_is_dokploy_server_generic_job() -> None:
    payload = hh.build_schedule_payload(server_id="null", script=_script())
    assert payload["scheduleType"] == "dokploy-server"
    assert payload["name"] == "finance-report-vps-host-hygiene"
    assert payload["serverId"] is None  # "null" -> default server
    assert payload["enabled"] is True
    assert payload["shellType"] == "bash"


class _FakeClient:
    def __init__(self, existing: list[dict] | None = None):
        self.existing = existing or []
        self.calls: list[tuple[str, str, dict | None]] = []

    def _request(self, method, endpoint, *, json=None, idempotent=False):
        self.calls.append((method, endpoint, json))
        if method == "GET":
            return self.existing
        return {"scheduleId": json.get("scheduleId") or "new-id"}


def test_ensure_creates_missing_named_job() -> None:
    client = _FakeClient(existing=[])
    sid = hh.ensure_host_hygiene_schedule(
        client,
        server_id="null",
        script=_script(),
        name="finance-report-vps-host-hygiene",
        cron_expression="17 3,9,15,21 * * *",
        timezone="Asia/Singapore",
        enabled=True,
    )
    endpoints = [e for _, e, _ in client.calls]
    assert any(e.startswith("schedule.list") for e in endpoints)
    assert "schedule.create" in endpoints
    assert "schedule.update" not in endpoints
    assert sid == "new-id"


def test_ensure_updates_existing_named_job() -> None:
    client = _FakeClient(
        existing=[{"name": "finance-report-vps-host-hygiene", "scheduleId": "sch-1"}]
    )
    hh.ensure_host_hygiene_schedule(
        client,
        server_id="null",
        script=_script(),
        name="finance-report-vps-host-hygiene",
        cron_expression="17 3,9,15,21 * * *",
        timezone="Asia/Singapore",
        enabled=True,
    )
    endpoints = [e for _, e, _ in client.calls]
    assert "schedule.update" in endpoints
    assert "schedule.create" not in endpoints
    # the update payload carries the resolved scheduleId
    update_payload = next(j for _, e, j in client.calls if e == "schedule.update")
    assert update_payload["scheduleId"] == "sch-1"


def test_main_ensure_provisions_via_get_dokploy(monkeypatch) -> None:
    from libs import dokploy as dokploy_module

    client = _FakeClient(existing=[])
    monkeypatch.setattr(dokploy_module, "get_dokploy", lambda *a, **k: client)
    rc = hh.main(["--ensure", "--server-id", "null"])
    assert rc == 0
    assert "schedule.create" in [e for _, e, _ in client.calls]


def test_main_emit_script_prints_without_api(capsys) -> None:
    rc = hh.main(["--emit-script"])
    assert rc == 0
    assert "docker image prune" in capsys.readouterr().out


if "pytest" not in sys.modules:  # pragma: no cover
    pass
