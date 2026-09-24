"""#895: the out-of-band watchdog's backup-freshness and restore-rehearsal checks.

Every red case here is a way the weekly backup or rehearsal has failed silently
before (#618 aborted runs, a Staging run overwriting the only pointer, the #892
rehearsal cron that had never run), so each must turn its signal red. The
watchdog's real remote commands run under a local `sh` against temp files, so a
wrong path, a broken pipeline or an undecodable byte fails here, not on the host.
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
from libs.backup.verification import load_backup_inventory
from tools import run_restore_rehearsal

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 28, 2, 17, tzinfo=UTC)
NOW_TS = int(NOW.timestamp())
CONFIG = watchdog.SshConfig(host="vps", user="root", port=22, key_path="/k")
PASS_LINE = run_restore_rehearsal.PASS_SUMMARY_PREFIX + ", ".join(
    run_restore_rehearsal.ALL_SERVICES
)
PASS_LOG = f"RESTORE_PROOF: PASS\n\n{PASS_LINE}\n"
PRODUCTION, STAGING = "infra2-backup-production", "infra2-backup-staging"
REHEARSAL = watchdog.RESTORE_REHEARSAL_CHECK
BACKUP_SIGNALS = {name for name, _ in watchdog.BACKUP_CHECKS} | {REHEARSAL}
SSH_NOISE = (
    "Warning: Permanently added '203.0.113.9' (ED25519) to the list of known hosts."
)


def _manifest(environment: str, *, drop=(), stale=(), created_at=None) -> dict:
    artifacts = []
    for entry in load_backup_inventory():
        if entry.service_id in drop:
            continue
        age_hours = entry.rpo_hours + 1 if entry.service_id in stale else 24
        artifacts.append(
            {
                "service_id": entry.service_id,
                "created_at": created_at or NOW_TS - age_hours * 3600,
                "size_bytes": 10,
                "sha256": "a" * 64,
                "remote_uri": f"{entry.remote}:infra2/weekly/{environment}/"
                f"20260927T033001Z/{entry.service_id}/archive.gz",
            }
        )
    return {"schema_version": 1, "environment": environment, "artifacts": artifacts}


class Host:
    """The VPS stand-in: manifests and the rehearsal log as real files."""

    def __init__(self, root: Path):
        self.root = root

    def manifest_path(self, environment: str) -> Path:
        return self.root / f"{environment}-manifest.json"

    def write_manifest(self, environment: str, content) -> None:
        if isinstance(content, dict):
            content = json.dumps(content)
        if isinstance(content, str):
            content = content.encode()
        self.manifest_path(environment).write_bytes(content)

    def write_rehearsal(self, text: str, *, age_hours: float) -> None:
        log = self.root / "rehearsal.log"
        log.write_text(text)
        mtime = NOW_TS - age_hours * 3600
        os.utime(log, (mtime, mtime))

    def healthy(self) -> Host:
        for environment in ("production", "staging"):
            self.write_manifest(environment, _manifest(environment))
        self.write_rehearsal(PASS_LOG, age_hours=22)
        return self

    def results(self) -> dict[str, watchdog.CheckResult]:
        results = watchdog.run_backup_checks(CONFIG, now=NOW)
        return {result.name: result for result in results}


@pytest.fixture
def host(tmp_path, monkeypatch) -> Host:
    stand_in = Host(tmp_path)
    monkeypatch.setattr(
        verification,
        "latest_manifest_path",
        lambda env: str(stand_in.manifest_path(env)),
    )
    monkeypatch.setattr(
        watchdog, "RESTORE_REHEARSAL_LOG", str(tmp_path / "rehearsal.log")
    )
    # The exact remote command string, run by a real shell instead of over ssh.
    monkeypatch.setattr(
        watchdog, "_ssh_argv", lambda _config, command: ["sh", "-c", command]
    )
    return stand_in


def test_a_healthy_host_is_green_for_every_backup_signal(host) -> None:
    results = host.healthy().results()

    assert set(results) == BACKUP_SIGNALS
    assert all(result.ok for result in results.values()), results
    total = len(load_backup_inventory())
    assert f"{total}/{total} artifacts" in results[PRODUCTION].detail
    assert results[REHEARSAL].detail == f"{PASS_LINE} (22.0h ago)"


def test_a_missing_manifest_is_red(host) -> None:
    host.healthy().manifest_path("staging").unlink()

    results = host.results()

    assert not results[STAGING].ok
    assert results[STAGING].detail.startswith("no staging manifest at ")
    assert results[PRODUCTION].ok


def test_a_staging_manifest_behind_the_production_pointer_is_red(host) -> None:
    host.healthy().write_manifest("production", _manifest("staging"))

    result = host.results()[PRODUCTION]

    assert not result.ok
    assert "environment 'staging'" in result.detail


def test_a_missing_or_stale_service_is_red_and_named(host) -> None:
    host.healthy().write_manifest(
        "production",
        _manifest("production", drop=["platform/minio"], stale=["truealpha/postgres"]),
    )

    detail = host.results()[PRODUCTION].detail

    assert "platform/minio: backup artifact is missing" in detail
    assert "truealpha/postgres: backup artifact is stale" in detail
    assert detail.startswith(f"2/{len(load_backup_inventory())} artifacts failed")


def test_a_future_timestamp_is_red_not_fresh(host) -> None:
    """Clock skew or milliseconds in a seconds field must not read as fresh."""
    host.healthy().write_manifest(
        "production", _manifest("production", created_at=NOW_TS * 1000)
    )
    host.write_rehearsal(PASS_LOG, age_hours=-30 * 24)

    results = host.results()

    assert not results[PRODUCTION].ok
    assert "backup artifact timestamp is in the future" in results[PRODUCTION].detail
    assert not results[REHEARSAL].ok
    assert "720.0h in the future" in results[REHEARSAL].detail


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"{trunc", "is not JSON"),
        (b"\xff\xfe{", "is not JSON"),
        (b"[]", "is not a JSON object"),
        (
            json.dumps({"environment": "production", "artifacts": None}),
            "backup check raised TypeError",
        ),
    ],
    ids=["truncated", "not-utf8", "array", "null-artifacts"],
)
def test_a_malformed_manifest_is_red_not_a_crash(host, content, expected) -> None:
    host.healthy().write_manifest("production", content)

    results = host.results()

    assert not results[PRODUCTION].ok
    assert expected in results[PRODUCTION].detail
    assert results[STAGING].ok and results[REHEARSAL].ok


def test_a_malformed_manifest_does_not_end_the_watchdog_run(
    host, monkeypatch, capsys
) -> None:
    """main() must still report and page for every other check."""
    host.healthy().write_manifest(
        "production", '{"environment": "production", "artifacts": null}'
    )
    for name in (
        "run_http_checks",
        "run_worker_status_check",
        "run_dokploy_status_check",
        "run_peer_scheduler_liveness_check",
    ):
        monkeypatch.setattr(watchdog, name, lambda *_args, **_kwargs: [])
    monkeypatch.setattr(watchdog, "run_ssh_checks", lambda _config, _targets: [])
    env = {
        "WATCHDOG_DRY_RUN": "1",
        "INFRA2_WATCHDOG_SSH_HOST": "vps",
        "INFRA2_WATCHDOG_SSH_USER": "root",
        "INFRA2_WATCHDOG_SSH_KEY_PATH": "/k",
    }
    with patch.object(watchdog, "datetime") as fake_datetime:
        fake_datetime.now.return_value = NOW

        assert watchdog.main(env) == 1

    out = capsys.readouterr().out
    assert f"FAIL {PRODUCTION}: backup check raised TypeError" in out
    assert f"OK {STAGING}" in out


def test_an_ssh_transport_failure_is_not_reported_as_a_missing_file() -> None:
    def unreachable(_command):
        refused = "ssh: connect to host vps port 22: Connection refused"
        return 255, "", f"{SSH_NOISE}\n{refused}"

    results = {
        result.name: result
        for result in watchdog.run_backup_checks(CONFIG, now=NOW, capture=unreachable)
    }

    for name in BACKUP_SIGNALS:
        assert not results[name].ok
        assert results[name].detail.startswith("could not reach the host")
        assert results[name].detail.endswith("Connection refused")
        assert "203.0.113.9" not in results[name].detail


def test_an_empty_inventory_is_red_not_zero_of_zero(host, monkeypatch) -> None:
    host.healthy()
    monkeypatch.setattr(verification, "load_backup_inventory", lambda: [])

    results = host.results()

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
    host, monkeypatch, error
) -> None:
    def broken():
        raise error

    host.healthy()
    monkeypatch.setattr(verification, "load_backup_inventory", broken)

    results = host.results()

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
    ("log", "age_hours", "expected"),
    [
        (None, 0, "has never run"),
        ("Traceback (most recent call last):\nAttributeError: x\n", 1, "did not pass"),
        ("[!] restore rehearsal FAILED for: truealpha/postgres\n", 1, "did not pass"),
        (
            run_restore_rehearsal.PASS_SUMMARY_PREFIX + "finance_report/postgres\n",
            1,
            "did not pass",
        ),
        ("", 1, "did not pass"),
        (PASS_LOG, 181, "181.0h ago (bound 180h)"),
    ],
    ids=["never-ran", "crashed", "failed", "partial", "empty", "stale"],
)
def test_a_rehearsal_that_did_not_prove_every_restore_is_red(
    host, log, age_hours, expected
) -> None:
    host.healthy()
    if log is None:
        (host.root / "rehearsal.log").unlink()
    else:
        host.write_rehearsal(log, age_hours=age_hours)

    result = host.results()[REHEARSAL]

    assert not result.ok
    assert expected in result.detail


def _rehearsal_output(capsys, statuses: dict[str, str]) -> str:
    def fake_run_rehearsal(*, service_id, **_kwargs):
        return {"status": statuses[service_id], "service_id": service_id}

    argv = ["run_restore_rehearsal.py", "--service-id", "all"]
    with (
        patch.object(run_restore_rehearsal, "run_rehearsal", fake_run_rehearsal),
        patch.object(sys, "argv", argv),
    ):
        run_restore_rehearsal.main()
    return capsys.readouterr().out


def test_the_rehearsal_tools_own_output_is_the_contract(host, capsys) -> None:
    """The watchdog parses what run_restore_rehearsal.main() actually prints."""
    everything = dict.fromkeys(run_restore_rehearsal.ALL_SERVICES, "PASS")
    passed = _rehearsal_output(capsys, everything)
    failed = _rehearsal_output(capsys, {**everything, "truealpha/postgres": "FAIL"})

    host.healthy().write_rehearsal(passed, age_hours=1)
    assert host.results()[REHEARSAL].ok
    host.write_rehearsal(failed, age_hours=1)
    assert not host.results()[REHEARSAL].ok


def test_severity_and_runbook_route_backup_failures() -> None:
    assert watchdog._severity_for(PRODUCTION, "backup") == "P1"
    assert watchdog._severity_for(STAGING, "backup") == "P2"
    assert watchdog._severity_for(REHEARSAL, "restore-rehearsal") == "P1"
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


#: Top-level modules the watchdog job's `pip install httpx python-dotenv rich PyYAML`
#: provides (measured in a clean Python 3.11 venv); anything else in site-packages
#: exists only in the test venv.
JOB_INSTALLED = {
    "_yaml",
    "anyio",
    "certifi",
    "dotenv",
    "h11",
    "httpcore",
    "httpx",
    "idna",
    "markdown_it",
    "mdurl",
    "pygments",
    "rich",
    "typing_extensions",
    "yaml",
}
_JOB_ONLY_IMPORTS = f"""
import importlib.machinery, sys
class JobOnly:
    def find_spec(self, name, path=None, target=None):
        spec = importlib.machinery.PathFinder.find_spec(name, path, target)
        if spec and "site-packages" in (spec.origin or "") and (
            name.split(".")[0] not in {sorted(JOB_INSTALLED)!r}
        ):
            raise ImportError(name + " is not installed in the watchdog job")
        return None
sys.meta_path.insert(0, JobOnly())
"""


def test_the_lazy_imports_resolve_with_only_the_jobs_packages() -> None:
    """A fresh interpreter, the repo root ahead of the stdlib, the job's packages only.

    The job runs the watchdog as a script, so the repo's platform/ package
    shadows the stdlib module on Linux (#892); pytest already holds the real one.
    A new third-party import on the backup path would make all three signals red
    every day in the job while the in-process tests stay green.
    """
    code = _JOB_ONLY_IMPORTS + (
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
    assert "verifier unavailable" not in result.stdout, result.stdout
    assert "no production manifest" in result.stdout
