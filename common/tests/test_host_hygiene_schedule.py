"""infra2 owns generic VPS host hygiene (Dokploy dokploy-server schedule provisioner)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

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


def test_preview_exclusion_pattern_matches_real_container_names() -> None:
    """The exclusion regex must actually match the real preview container names
    (finance_report-<service><ENV_SUFFIX>) across every alias kind, or generic
    hygiene would prune live previews."""
    rx = re.compile(hh.PR_PREVIEW_CONTAINER_PATTERN)
    # service container_names from finance_report/preview/compose.yaml × alias kinds
    must_match = [
        "finance_report-backend-pr-84",
        "finance_report-frontend-pr-84",
        "finance_report-preview-db-pr-84",
        "finance_report-app-vault-agent-pr-84",
        "finance_report-backend-branch-main",
    ]
    for name in must_match:
        assert rx.match(name), f"preview container not excluded: {name}"
    # non-preview containers must NOT be excluded (they stay eligible for hygiene)
    must_not_match = [
        "finance_report-postgres",  # shared platform service, not a preview
        "finance_report-backend-staging",  # fixed env, not a preview alias kind
        "some-random-stopped-container",
        "finance_report-backend",  # no ENV_SUFFIX
    ]
    for name in must_not_match:
        assert not rx.match(name), f"non-preview wrongly excluded: {name}"


def test_preview_exclusion_pattern_covers_every_alias_kind() -> None:
    # derived from the canonical PREVIEW_KINDS — a new kind can't silently fall out
    from common.deploy.env_config import PREVIEW_KINDS

    rx = re.compile(hh.PR_PREVIEW_CONTAINER_PATTERN)
    for kind in PREVIEW_KINDS:
        assert rx.match(f"finance_report-backend-{kind}-x"), kind


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
    from common.deploy import dokploy_client as dokploy_module

    client = _FakeClient(existing=[])
    monkeypatch.setattr(dokploy_module, "get_dokploy", lambda *a, **k: client)
    rc = hh.main(["--ensure", "--server-id", "null"])
    assert rc == 0
    assert "schedule.create" in [e for _, e, _ in client.calls]


def test_main_emit_script_prints_without_api(capsys) -> None:
    rc = hh.main(["--emit-script"])
    assert rc == 0
    assert "docker image prune" in capsys.readouterr().out


def test_default_image_prune_keeps_unused_images_for_at_most_24_hours(capsys) -> None:
    """#1621: a 72 h limit freed 0 bytes at several releases per day (disk at 81 %)."""
    match = re.fullmatch(r"(\d+)h", hh.DEFAULT_IMAGE_PRUNE_UNTIL)
    assert match is not None, hh.DEFAULT_IMAGE_PRUNE_UNTIL
    assert 1 <= int(match.group(1)) <= 24

    assert hh.main(["--emit-script"]) == 0
    prune_lines = [
        line
        for line in capsys.readouterr().out.splitlines()
        if "docker image prune -af" in line
    ]
    assert len(prune_lines) == 1, prune_lines
    for line in prune_lines:
        assert f"until={hh.DEFAULT_IMAGE_PRUNE_UNTIL}" in line, line
        assert "until=72h" not in line, line


def test_default_builder_prune_keeps_cache_for_at_most_24_hours(capsys) -> None:
    """#1624: on 2026-11-12 the build cache held 5.7 GB; `until=24h` freed 5.69 GB."""
    match = re.fullmatch(r"(\d+)h", hh.DEFAULT_BUILDER_PRUNE_UNTIL)
    assert match is not None, hh.DEFAULT_BUILDER_PRUNE_UNTIL
    assert 1 <= int(match.group(1)) <= 24

    assert hh.main(["--emit-script"]) == 0
    prune_lines = [
        line
        for line in capsys.readouterr().out.splitlines()
        if "docker builder prune -af" in line
    ]
    assert len(prune_lines) == 1, prune_lines
    for line in prune_lines:
        assert f"until={hh.DEFAULT_BUILDER_PRUNE_UNTIL}" in line, line
        assert "until=72h" not in line, line


def _manifest_line(batch: str, status: str, purge_after: int, name: str) -> str:
    record = {
        "batch_id": batch,
        "original_path": f"/root/{name}",
        "purge_after_epoch": purge_after,
        "status": status,
    }
    return json.dumps(record) + "\n"


def _run_purge_step(quarantine: Path) -> subprocess.CompletedProcess[str]:
    """Run only the quarantine step of the scheduled script, as the host shell does."""
    script = "\n".join(
        ["set -eu", *hh.build_quarantine_purge_step(dry_run=False), "echo AFTER-STEP"]
    )
    return subprocess.run(
        ["bash", "-c", script],
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "INFRA2_QUARANTINE_DIR": str(quarantine),
        },
        text=True,
        capture_output=True,
        timeout=60,
    )


def test_scheduled_script_purges_an_expired_mixed_batch_and_keeps_a_live_one(
    tmp_path: Path,
) -> None:
    """#1624: the purge ran only by hand and skipped a batch with an archived record."""
    now = int(time.time())
    quarantine = tmp_path / "quarantine"
    for batch in ("old", "new"):
        (quarantine / batch / "files").mkdir(parents=True)
        (quarantine / batch / "files" / "payload").write_text("x", encoding="utf-8")
    (quarantine / "manifest.jsonl").write_text(
        _manifest_line("old", "archived_to_gdrive_and_purged", now - 99, "a")
        + _manifest_line("old", "quarantined", now - 99, "b")
        + _manifest_line("new", "quarantined", now + 86_400, "c"),
        encoding="utf-8",
    )

    proc = _run_purge_step(quarantine)

    assert proc.returncode == 0, proc.stderr
    assert "purged quarantine batch old" in proc.stdout
    assert not (quarantine / "old").exists()
    assert (quarantine / "new" / "files" / "payload").exists()
    statuses = [
        json.loads(line)["status"]
        for line in (quarantine / "manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert sorted(statuses) == [
        "archived_to_gdrive_and_purged",
        "purged",
        "quarantined",
    ]


def test_a_failing_quarantine_purge_does_not_stop_the_hygiene_script(
    tmp_path: Path,
) -> None:
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    (quarantine / "manifest.jsonl").write_text("not json\n", encoding="utf-8")

    proc = _run_purge_step(quarantine)

    assert proc.returncode == 0, proc.stderr
    assert "::warning::quarantine purge failed" in proc.stdout
    assert "AFTER-STEP" in proc.stdout


def test_a_host_without_a_quarantine_manifest_skips_the_purge(tmp_path: Path) -> None:
    proc = _run_purge_step(tmp_path / "missing")
    assert proc.returncode == 0, proc.stderr
    assert "skipping quarantine purge" in proc.stdout
    assert "AFTER-STEP" in proc.stdout


def test_the_full_script_embeds_the_one_shared_purge_rule() -> None:
    source = hh.QUARANTINE_PURGE_SOURCE.read_text(encoding="utf-8")
    assert "def purge_expired" in source
    assert source.strip() in _script()
    assert source.strip() not in _script(dry_run=True)
    assert "[dry-run] purge expired quarantine batches" in _script(dry_run=True)


def test_the_shell_never_expands_the_embedded_purge_text(
    tmp_path: Path, monkeypatch
) -> None:
    """The heredoc terminator is quoted: `$(...)` and backticks in the file stay text."""
    marker = tmp_path / "expanded"
    source = tmp_path / "purge.py"
    source.write_text(
        f'"""A `touch {marker}` and $(touch {marker}) inside the embedded file."""\n'
        "import sys\n"
        'print("embedded file ran", sys.argv[1:] != [])\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(hh, "QUARANTINE_PURGE_SOURCE", source)
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    (quarantine / "manifest.jsonl").write_text("", encoding="utf-8")

    proc = _run_purge_step(quarantine)

    assert proc.returncode == 0, proc.stderr
    assert "embedded file ran True" in proc.stdout
    assert not marker.exists(), "the shell expanded text of the embedded file"


def test_a_purge_file_with_a_line_equal_to_the_terminator_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "purge.py"
    source.write_text(
        f"print(1)\n{hh.QUARANTINE_HEREDOC_END}\nprint(2)\n", encoding="utf-8"
    )
    monkeypatch.setattr(hh, "QUARANTINE_PURGE_SOURCE", source)
    with pytest.raises(ValueError, match="heredoc terminator"):
        hh.build_quarantine_purge_step(dry_run=False)


def test_the_real_purge_file_never_ends_the_heredoc_early() -> None:
    lines = hh.QUARANTINE_PURGE_SOURCE.read_text(encoding="utf-8").splitlines()
    assert hh.QUARANTINE_HEREDOC_END not in lines
    step = hh.build_quarantine_purge_step(dry_run=False)
    assert step.count(hh.QUARANTINE_HEREDOC_END) == 1, "one terminator line, at the end"


if "pytest" not in sys.modules:  # pragma: no cover
    pass
