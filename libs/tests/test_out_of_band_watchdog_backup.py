"""#895: the out-of-band watchdog's backup-freshness and restore-rehearsal checks.

Every red case here is a way the weekly backup or rehearsal has failed silently
before (#618 aborted runs, a Staging run overwriting the only pointer, the #892
rehearsal cron that had never run), so each must turn its signal red.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import libs.backup.verification as verification
import tools.out_of_band_watchdog as watchdog
from libs.backup.verification import latest_manifest_path, load_backup_inventory
from tools import run_restore_rehearsal

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 28, 2, 17, tzinfo=UTC)
NOW_TS = int(NOW.timestamp())
CONFIG = watchdog.SshConfig(host="vps", user="root", port=22, key_path="/k")
PASS_LINE = run_restore_rehearsal.PASS_SUMMARY_PREFIX + ", ".join(
    run_restore_rehearsal.ALL_SERVICES
)
SSH_NOISE = (
    "Warning: Permanently added '203.0.113.9' (ED25519) to the list of known hosts."
)
BACKUP_SIGNALS = {name for name, _ in watchdog.BACKUP_CHECKS} | {
    watchdog.RESTORE_REHEARSAL_CHECK
}


def _manifest(environment: str, *, drop=(), stale=()) -> dict:
    artifacts = []
    for entry in load_backup_inventory():
        if entry.service_id in drop:
            continue
        age_hours = entry.rpo_hours + 1 if entry.service_id in stale else 24
        artifacts.append(
            {
                "service_id": entry.service_id,
                "created_at": NOW_TS - age_hours * 3600,
                "size_bytes": 10,
                "sha256": "a" * 64,
                "remote_uri": f"{entry.remote}:infra2/weekly/{environment}/"
                f"20260927T033001Z/{entry.service_id}/archive.gz",
            }
        )
    return {"schema_version": 1, "environment": environment, "artifacts": artifacts}


class FakeHost:
    """Answers the watchdog's remote commands the way the VPS shell would."""

    def __init__(self, files: dict[str, str], rehearsal: tuple[int, str] | None):
        self.files = files
        self.rehearsal = rehearsal

    def __call__(self, command: str) -> tuple[int, str, str]:
        if command.startswith("cat "):
            path = command.split(" ", 1)[1]
            if path in self.files:
                return 0, self.files[path], ""
            return 1, "", f"{SSH_NOISE}\ncat: {path}: No such file or directory"
        if command.startswith("stat "):
            if self.rehearsal is None:
                return 1, "", "stat: cannot statx: No such file or directory"
            mtime, text = self.rehearsal
            return 0, f"{mtime}\n" + "\n".join(text.splitlines()[-5:]) + "\n", ""
        raise AssertionError(f"unexpected remote command: {command}")


def _host(**overrides) -> FakeHost:
    files = {
        latest_manifest_path(env): json.dumps(_manifest(env))
        for env in ("production", "staging")
    }
    files.update(overrides.pop("files", {}))
    for path in overrides.pop("absent", ()):
        files.pop(path)
    rehearsal = overrides.pop(
        "rehearsal", (NOW_TS - 22 * 3600, f"RESTORE_PROOF: PASS\n\n{PASS_LINE}\n")
    )
    assert not overrides
    return FakeHost(files, rehearsal)


def _results(host: FakeHost) -> dict[str, watchdog.CheckResult]:
    results = watchdog.run_backup_checks(CONFIG, now=NOW, capture=host)
    return {result.name: result for result in results}


def test_a_healthy_host_is_green_for_every_backup_signal() -> None:
    results = _results(_host())

    assert set(results) == BACKUP_SIGNALS
    assert all(result.ok for result in results.values()), results
    total = len(load_backup_inventory())
    assert f"{total}/{total} artifacts" in results["infra2-backup-production"].detail


def test_a_missing_manifest_is_red() -> None:
    results = _results(_host(absent=[latest_manifest_path("staging")]))

    detail = results["infra2-backup-staging"].detail
    assert not results["infra2-backup-staging"].ok
    assert detail.endswith("staging-manifest.json: No such file or directory")
    assert "203.0.113.9" not in detail
    assert results["infra2-backup-production"].ok


def test_a_staging_manifest_behind_the_production_pointer_is_red() -> None:
    wrong = json.dumps(_manifest("staging"))
    results = _results(_host(files={latest_manifest_path("production"): wrong}))

    assert not results["infra2-backup-production"].ok
    assert "environment 'staging'" in results["infra2-backup-production"].detail


def test_a_missing_or_stale_service_is_red_and_named() -> None:
    manifest = _manifest(
        "production", drop=["platform/minio"], stale=["truealpha/postgres"]
    )
    results = _results(
        _host(files={latest_manifest_path("production"): json.dumps(manifest)})
    )

    detail = results["infra2-backup-production"].detail
    assert not results["infra2-backup-production"].ok
    assert "platform/minio: backup artifact is missing" in detail
    assert "truealpha/postgres: backup artifact is stale" in detail
    assert detail.startswith(f"2/{len(load_backup_inventory())} artifacts failed")


def test_a_manifest_that_is_not_json_is_red() -> None:
    results = _results(_host(files={latest_manifest_path("production"): "{trunc"}))

    assert not results["infra2-backup-production"].ok
    assert "is not JSON" in results["infra2-backup-production"].detail


def test_an_empty_inventory_is_red_not_zero_of_zero(monkeypatch) -> None:
    monkeypatch.setattr(verification, "load_backup_inventory", lambda: [])

    results = _results(_host())

    for name, _ in watchdog.BACKUP_CHECKS:
        assert not results[name].ok
        assert results[name].detail == "backup inventory is empty"


@pytest.mark.parametrize(
    "error",
    [
        ModuleNotFoundError("No module named 'yaml'"),
        ValueError("duplicate backup inventory id 'platform/redis'"),
        AttributeError("module 'platform' has no attribute 'system'"),
    ],
    ids=["no-pyyaml", "bad-inventory", "shadowed-stdlib"],
)
def test_a_broken_verifier_is_red_for_every_signal_not_a_crash(
    monkeypatch, error
) -> None:
    def broken():
        raise error

    monkeypatch.setattr(verification, "load_backup_inventory", broken)

    results = _results(_host())

    assert set(results) == BACKUP_SIGNALS
    for result in results.values():
        assert not result.ok
        assert result.failure_domain == "configuration"
        assert f"backup verifier unavailable: {type(error).__name__}" in result.detail


def test_missing_ssh_config_is_red_for_every_signal() -> None:
    results = watchdog.run_backup_checks(None, now=NOW)

    assert {result.name for result in results} == BACKUP_SIGNALS
    assert not any(result.ok for result in results)


@pytest.mark.parametrize(
    ("rehearsal", "expected"),
    [
        (None, "has never run"),
        (
            (NOW_TS - 3600, "Traceback (most recent call last):\nAttributeError: x"),
            "did not pass every service",
        ),
        (
            (NOW_TS - 3600, "[!] restore rehearsal FAILED for: truealpha/postgres"),
            "did not pass every service",
        ),
        (
            (
                NOW_TS - 3600,
                run_restore_rehearsal.PASS_SUMMARY_PREFIX + "finance_report/postgres",
            ),
            "did not pass every service",
        ),
        ((NOW_TS - 181 * 3600, PASS_LINE), "181.0h ago (bound 180h)"),
    ],
    ids=["never-ran", "crashed", "failed", "partial", "stale"],
)
def test_a_rehearsal_that_did_not_prove_every_restore_is_red(
    rehearsal, expected
) -> None:
    result = _results(_host(rehearsal=rehearsal))[watchdog.RESTORE_REHEARSAL_CHECK]

    assert not result.ok
    assert expected in result.detail


def _rehearsal_log(capsys, statuses: dict[str, str]) -> str:
    def fake_run_rehearsal(*, service_id, **_kwargs):
        return {"status": statuses[service_id], "service_id": service_id}

    argv = ["run_restore_rehearsal.py", "--service-id", "all"]
    with (
        patch.object(run_restore_rehearsal, "run_rehearsal", fake_run_rehearsal),
        patch.object(sys, "argv", argv),
    ):
        run_restore_rehearsal.main()
    return capsys.readouterr().out


def test_the_rehearsal_tools_own_output_is_the_contract(capsys) -> None:
    """The watchdog parses what run_restore_rehearsal.main() actually prints."""
    everything = dict.fromkeys(run_restore_rehearsal.ALL_SERVICES, "PASS")
    passed = _rehearsal_log(capsys, everything)
    failed = _rehearsal_log(capsys, {**everything, "truealpha/postgres": "FAIL"})

    check = watchdog.RESTORE_REHEARSAL_CHECK
    assert _results(_host(rehearsal=(NOW_TS, passed)))[check].ok
    assert not _results(_host(rehearsal=(NOW_TS, failed)))[check].ok


def test_severity_and_runbook_route_backup_failures() -> None:
    assert watchdog._severity_for("infra2-backup-production", "backup") == "P1"
    assert watchdog._severity_for("infra2-backup-staging", "backup") == "P2"
    assert (
        watchdog._severity_for(watchdog.RESTORE_REHEARSAL_CHECK, "restore-rehearsal")
        == "P1"
    )
    headings = {
        re.sub(r"[^\w\- ]", "", line.lstrip("#").strip().lower()).replace(" ", "-")
        for line in (ROOT / "docs/ssot/ops.recovery.md").read_text().splitlines()
        if line.startswith("#")
    }
    for domain in ("backup", "restore-rehearsal"):
        url = watchdog._runbook_url_for_failure(domain)
        assert url.split("#", 1)[1] in headings, url


def test_the_watchdog_job_installs_what_the_backup_checks_import() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/ops-checks.yml").read_text(encoding="utf-8")
    )
    [job] = [
        job
        for job in workflow["jobs"].values()
        if any(
            step.get("run", "").strip() == "python tools/out_of_band_watchdog.py"
            for step in job.get("steps", [])
        )
    ]
    installs = " ".join(
        step["run"] for step in job["steps"] if "pip install" in step.get("run", "")
    )
    assert "PyYAML" in installs.split()


def test_the_lazy_imports_resolve_in_a_fresh_interpreter() -> None:
    """The job runs the watchdog as a script, so the repo root precedes the stdlib.

    The repo's platform/ package then shadows the stdlib module on Linux (#892);
    pytest already holds the real one, so only a fresh interpreter shows it.
    """
    code = (
        "import tools.out_of_band_watchdog as w\n"
        "results = w.run_backup_checks(w.SshConfig('h', 'u', 22, 'k'),"
        " capture=lambda command: (1, '', 'absent'))\n"
        "print('\\n'.join(r.detail for r in results))\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONSAFEPATH"}
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={**env, "PYTHONPATH": "."},
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "verifier unavailable" not in result.stdout
    assert "no production manifest" in result.stdout
