"""Tests for tools.staging_db_hygiene and its ops-checks workflow wiring (#1624 AC2)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tools.staging_db_hygiene import (
    DEFAULT_RETENTION_DAYS,
    STAGING_BACKEND_CONTAINER,
    STAGING_POSTGRES_CONTAINER,
    build_remote_command,
    main,
    run_staging_db_hygiene,
)

ROOT = Path(__file__).resolve().parents[2]


def test_build_remote_command_targets_only_staging_and_respects_apply() -> None:
    backend_compose = yaml.safe_load(
        (ROOT / "finance_report/finance_report/10.app/compose.yaml").read_text(
            encoding="utf-8"
        )
    )
    postgres_compose = yaml.safe_load(
        (ROOT / "finance_report/finance_report/01.postgres/compose.yaml").read_text(
            encoding="utf-8"
        )
    )
    expected_backend = backend_compose["services"]["backend"]["container_name"].replace(
        "${ENV_SUFFIX}", "-staging"
    )
    expected_postgres = postgres_compose["services"]["postgres"][
        "container_name"
    ].replace("${ENV_SUFFIX}", "-staging")
    assert STAGING_BACKEND_CONTAINER == expected_backend
    assert STAGING_POSTGRES_CONTAINER == expected_postgres

    dry = build_remote_command(older_than_days=DEFAULT_RETENTION_DAYS, apply=False)
    assert f"docker exec {expected_backend} sh -c " in dry
    assert ". /secrets/.env" in dry
    assert f"docker exec {expected_postgres} " in dry
    assert "--rotate-append-only --older-than-days 14" in dry
    assert "--apply" not in dry
    assert "finance_report-backend " not in dry
    assert "finance_report-postgres " not in dry

    applied = build_remote_command(older_than_days=14, apply=True)
    assert "--rotate-append-only --older-than-days 14 --apply" in applied


def test_build_remote_command_rejects_non_positive_days() -> None:
    with pytest.raises(ValueError, match="older_than_days"):
        build_remote_command(older_than_days=0, apply=False)


def test_run_staging_db_hygiene_forwards_ssh_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[tuple[str, str, str, str, int]] = []

    def fake_ssh(cmd: str, host: str, user: str, key: str, port: int):
        seen.append((cmd, host, user, key, port))
        return 0, "=== PURGE ===\nok\n", ""

    monkeypatch.setenv("INFRA2_WATCHDOG_SSH_HOST", "1.2.3.4")
    rc = run_staging_db_hygiene(older_than_days=14, apply=True, runner=fake_ssh)
    assert rc == 0
    assert len(seen) == 1
    assert "--apply" in seen[0][0]
    assert seen[0][1] == "1.2.3.4"
    assert "=== PURGE ===" in capsys.readouterr().out

    def fail_ssh(*_a):
        return 2, "", "ssh failed"

    assert run_staging_db_hygiene(runner=fail_ssh) == 2


def test_cli_parses_apply_and_retention(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[int, bool]] = []

    def fake_run(*, older_than_days: int, apply: bool) -> int:
        captured.append((older_than_days, apply))
        return 0

    monkeypatch.setattr("tools.staging_db_hygiene.run_staging_db_hygiene", fake_run)
    assert main(["--older-than-days", "14", "--apply"]) == 0
    assert main([]) == 0
    assert captured == [(14, True), (14, False)]


def test_ops_checks_wires_staging_db_hygiene_with_dry_run_guard() -> None:
    doc = yaml.safe_load(
        (ROOT / ".github/workflows/ops-checks.yml").read_text(encoding="utf-8")
    )
    assert "47 3 * * *" in [c["cron"] for c in doc["on"]["schedule"]]
    assert (
        "staging-db-hygiene"
        in doc["on"]["workflow_dispatch"]["inputs"]["task"]["options"]
    )
    job = doc["jobs"]["staging-db-hygiene"]
    run_text = "\n".join(step.get("run", "") for step in job["steps"])
    assert "python -m tools.staging_db_hygiene" in run_text
    assert "--older-than-days 14" in run_text
    assert "--apply" in run_text
    assert "dry_run" in run_text
