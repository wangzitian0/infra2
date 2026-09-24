"""The weekly stability report runner (#904): VPS ledgers over SSH + the Worker's outages."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import urllib.error
from datetime import UTC, datetime
from pathlib import Path

import pytest

from libs.observability import local_ledger
from libs.observability.local_ledger import host_ledger_path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "tools" / "stability_report.py"
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
DAY0 = datetime(2026, 9, 24, tzinfo=UTC).timestamp()


def _ledger(
    environment: str,
    *,
    fail_every: int = 0,
    until: float | None = None,
    hole: tuple[float, float] | None = None,
) -> dict:
    """A ledger written the way the runner writes it: a round every 60 s from
    midnight until ``until`` (default: a minute before NOW), none inside ``hole``."""
    ledger = local_ledger.new_ledger(environment)
    at = DAY0
    end = NOW.timestamp() - 60 if until is None else until
    count = 0
    while at < end:
        if not (hole and hole[0] <= at < hole[1]):
            count += 1
            ok = not (fail_every and count % fail_every == 0)
            local_ledger.record_round(
                ledger,
                {"public-route": [{"spec": {"name": "minio-public-route"}, "ok": ok}]},
                environment,
                at,
            )
        at += 60
    return ledger


def _outage_ms(hour: int, minute: int) -> int:
    return int(datetime(2026, 9, 24, hour, minute, tzinfo=UTC).timestamp() * 1000)


OUTAGES = {
    "ok": True,
    "outages": [
        {
            "environment": "production",
            "start": _outage_ms(3, 0),
            "end": _outage_ms(3, 45),
        }
    ],
}


def _load_module():
    spec = importlib.util.spec_from_file_location("stability_report", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("Failed to load stability_report module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _files(tmp_path, production: dict, staging: dict, outages: dict) -> dict:
    paths = {}
    for name, doc in (
        ("production", production),
        ("staging", staging),
        ("outages", outages),
    ):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(doc))
        paths[name] = str(path)
    return paths


# ---- 正例 ---------------------------------------------------------------------


def test_positive_dry_run_reports_production_and_a_staging_line(
    tmp_path, capsys
) -> None:
    report = _load_module()
    paths = _files(
        tmp_path, _ledger("production"), _ledger("staging", fail_every=100), OUTAGES
    )

    rc = report.run(
        {"INFRA2_STABILITY_REPORT_DRY_RUN": "1"},
        ledger_files={"production": paths["production"], "staging": paths["staging"]},
        outages_file=paths["outages"],
        now=NOW,
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "[STABILITY]" in out
    assert "window 1d | 719 probe runs" in out
    # the runner saw production perfect; the Worker's 45-minute outage says otherwise
    assert "production unavailable 45.0 min, counted as failures" in out
    assert "45.0 min unreachable per Cloudflare in 1 outage(s)" in out
    assert "production:minio-public-route" in out
    assert "Signals at 100%: 0/1" in out
    (staging,) = [line for line in out.splitlines() if line.startswith("staging ")]
    assert staging.startswith("staging (reported, never paged): 99.026% · 0/1")
    assert "lowest staging:minio-public-route" in staging


def test_negative_a_45_minute_stop_of_the_runner_is_45_minutes_unavailable(
    tmp_path, capsys
) -> None:
    """#911 review: shorter than the Worker's staleness window, so no outage edge."""
    report = _load_module()
    hole = (DAY0 + 3 * 3600, DAY0 + 3 * 3600 + 45 * 60)
    paths = _files(
        tmp_path,
        _ledger("production", hole=hole),
        _ledger("staging"),
        {"ok": True, "outages": []},
    )
    assert (
        report.run(
            {"INFRA2_STABILITY_REPORT_DRY_RUN": "1"},
            ledger_files={
                "production": paths["production"],
                "staging": paths["staging"],
            },
            outages_file=paths["outages"],
            now=NOW,
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "production unavailable 45.0 min" in out
    assert "45.0 min with no probe round" in out
    # 719 minutes to 11:59: 674 rounds recorded + 45 missed checks
    assert "production:minio-public-route: 93.741% (45 fail/719 checks)" in out


def test_positive_the_vps_is_read_over_ssh_and_outages_from_the_worker(
    monkeypatch, capsys
) -> None:
    report = _load_module()
    commands: list[str] = []

    def fake_run(argv, **_kwargs):
        commands.append(argv[-1])
        environment = "staging" if "-staging/" in argv[-1] else "production"
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(_ledger(environment)), ""
        )

    seen: dict[str, str] = {}

    def fake_fetch(url: str, token: str, *, timeout: float = 20.0) -> list:
        seen.update(url=url, token=token)
        return OUTAGES["outages"]

    monkeypatch.setattr(report.subprocess, "run", fake_run)
    monkeypatch.setattr(report, "fetch_outages", fake_fetch)
    env = {
        "INFRA2_STABILITY_REPORT_DRY_RUN": "1",
        "INFRA2_WATCHDOG_SSH_HOST": "vps.example.invalid",
        "INFRA2_WATCHDOG_SSH_USER": "watchdog",
        "INFRA2_WATCHDOG_SSH_KEY_PATH": "/tmp/key",
        "INFRA2_WATCHDOG_WORKER_STATUS_TOKEN": "tok",
    }

    assert report.run(env, now=NOW) == 0
    assert commands == [
        f"cat {host_ledger_path('production')}",
        f"cat {host_ledger_path('staging')}",
    ]
    # no configured URL: the public Worker default, never a silent skip (#1851 G4)
    assert seen == {"url": report.DEFAULT_OUTAGES_URL, "token": "tok"}
    assert "45.0 min" in capsys.readouterr().out


def test_an_unreadable_staging_ledger_over_ssh_is_reported(monkeypatch, capsys) -> None:
    report = _load_module()

    def fake_run(argv, **_kwargs):
        if "-staging/" in argv[-1]:
            return subprocess.CompletedProcess(argv, 1, "", "No such file")
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(_ledger("production")), ""
        )

    monkeypatch.setattr(report.subprocess, "run", fake_run)
    monkeypatch.setattr(report, "fetch_outages", lambda *_a, **_k: [])
    env = {
        "INFRA2_STABILITY_REPORT_DRY_RUN": "1",
        "INFRA2_WATCHDOG_SSH_HOST": "vps.example.invalid",
        "INFRA2_WATCHDOG_SSH_USER": "watchdog",
        "INFRA2_WATCHDOG_SSH_KEY_PATH": "/tmp/key",
    }
    assert report.run(env, now=NOW) == 0
    out = capsys.readouterr().out
    assert "staging (reported, never paged): ledger unavailable: reading staging" in out


# ---- 反例: every missing input fails loudly --------------------------------------


def _run_with(tmp_path, production: dict, staging: dict, outages: dict = OUTAGES):
    report = _load_module()
    paths = _files(tmp_path, production, staging, outages)
    return report.run(
        {"INFRA2_STABILITY_REPORT_DRY_RUN": "1"},
        ledger_files={"production": paths["production"], "staging": paths["staging"]},
        outages_file=paths["outages"],
        now=NOW,
    )


def test_negative_an_empty_ledger_is_not_a_perfect_week(tmp_path) -> None:
    """GREEN-WHILE-EMPTY: zero signals must never render as 100%."""
    empty = _ledger("production")
    empty["days"] = {}
    with pytest.raises(RuntimeError, match="production ledger has no recorded signals"):
        _run_with(tmp_path, empty, _ledger("staging"))


def test_negative_a_stopped_production_ledger_writer_is_not_fresh(tmp_path) -> None:
    """STALE-REPORTED-AS-FRESH: a ledger nobody writes any more fails the run."""
    stale = _ledger("production", until=NOW.timestamp() - 7 * 3600)
    with pytest.raises(RuntimeError, match="production ledger is stale"):
        _run_with(tmp_path, stale, _ledger("staging"))


def test_a_stale_staging_ledger_is_a_line_not_a_failure(tmp_path, capsys) -> None:
    """#911 review: staging must not withhold the production report."""
    stale = _ledger("staging", until=NOW.timestamp() - 7 * 3600)
    assert _run_with(tmp_path, _ledger("production"), stale) == 0
    out = capsys.readouterr().out
    assert "Overall availability" in out
    assert (
        "staging (reported, never paged): ledger unusable: staging ledger is stale"
        in out
    )


def test_a_missing_staging_ledger_is_a_line_not_a_failure(tmp_path, capsys) -> None:
    report = _load_module()
    paths = _files(tmp_path, _ledger("production"), _ledger("staging"), OUTAGES)
    assert (
        report.run(
            {"INFRA2_STABILITY_REPORT_DRY_RUN": "1"},
            ledger_files={"production": paths["production"]},
            outages_file=paths["outages"],
            now=NOW,
        )
        == 0
    )
    assert "staging (reported, never paged): ledger unavailable: not read" in (
        capsys.readouterr().out
    )


def test_negative_a_missing_production_ledger_fails(tmp_path) -> None:
    report = _load_module()
    paths = _files(tmp_path, _ledger("production"), _ledger("staging"), OUTAGES)
    with pytest.raises(RuntimeError, match="production ledger is missing"):
        report.run(
            {"INFRA2_STABILITY_REPORT_DRY_RUN": "1"},
            ledger_files={"staging": paths["staging"]},
            outages_file=paths["outages"],
            now=NOW,
        )


def test_negative_an_outage_response_without_a_list_fails(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="have no `outages` list"):
        _run_with(tmp_path, _ledger("production"), _ledger("staging"), {"ok": True})


def test_negative_an_ssh_failure_names_the_environment_and_path(monkeypatch) -> None:
    report = _load_module()
    monkeypatch.setattr(
        report.subprocess,
        "run",
        lambda argv, **_kw: subprocess.CompletedProcess(
            argv, 1, "", "No such file or directory"
        ),
    )
    env = {
        "INFRA2_WATCHDOG_SSH_HOST": "vps.example.invalid",
        "INFRA2_WATCHDOG_SSH_USER": "watchdog",
        "INFRA2_WATCHDOG_SSH_KEY_PATH": "/tmp/key",
    }
    with pytest.raises(RuntimeError) as error:
        report.run(env, now=NOW)
    assert (
        f"reading production ledger {host_ledger_path('production')} over SSH exited 1"
        in str(error.value)
    )
    assert "No such file or directory" in str(error.value)


def test_negative_no_ssh_config_fails_before_reporting(monkeypatch) -> None:
    report = _load_module()
    with pytest.raises(
        RuntimeError, match="INFRA2_WATCHDOG_SSH_HOST / _USER / _KEY_PATH"
    ):
        report.run({}, now=NOW)


def test_negative_empty_outages_default_still_fails_loudly(monkeypatch, capsys) -> None:
    """Defensive: if DEFAULT_OUTAGES_URL were ever cleared, fail (rc=2), never skip."""
    report = _load_module()
    monkeypatch.setattr(report, "DEFAULT_OUTAGES_URL", "")
    assert report.run({}, now=NOW) == 2
    assert "required" in capsys.readouterr().err


def test_fetch_outages_names_itself_and_surfaces_the_refusing_layer(
    monkeypatch,
) -> None:
    """Cloudflare answers urllib's default User-Agent with `403 error code: 1010` before
    the worker sees the request (2026-09-08); the digest died on it two Mondays running."""
    report = _load_module()
    seen: dict[str, object] = {}

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def fake_urlopen(request, timeout):
        seen["user_agent"] = request.get_header("User-agent")
        seen["authorization"] = request.get_header("Authorization")
        return _Response(json.dumps(OUTAGES).encode())

    monkeypatch.setattr(report, "urlopen", fake_urlopen)
    assert (
        report.fetch_outages("https://w.example/outages", "tok") == OUTAGES["outages"]
    )
    assert seen == {
        "user_agent": "infra2-stability-report/1.0",
        "authorization": "Bearer tok",
    }

    def refusing_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            403,
            "Forbidden",
            hdrs=None,
            fp=io.BytesIO(b"error code: 1010"),
        )

    monkeypatch.setattr(report, "urlopen", refusing_urlopen)
    with pytest.raises(RuntimeError) as error:
        report.fetch_outages("https://w.example/outages", "tok")
    assert "HTTP 403" in str(error.value) and "error code: 1010" in str(error.value)


def test_the_cli_parses_one_ledger_per_environment(tmp_path, capsys) -> None:
    report = _load_module()
    paths = _files(tmp_path, _ledger("production"), _ledger("staging"), OUTAGES)
    with pytest.raises(SystemExit):
        report.main(["--ledger", f"qa={paths['production']}"], env={})
    assert "expected ENV=PATH" in capsys.readouterr().err
