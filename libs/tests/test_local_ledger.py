"""The VPS-side availability ledger (#904): what the probe runner counts, keeps and writes."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

import libs.observability.probes as probes
from libs.observability import local_ledger

ROOT = Path(__file__).resolve().parents[2]
DAY = 86400
T0 = datetime(2026, 9, 24, 12, 0, tzinfo=UTC).timestamp()


def _result(name: str, ok: bool, severity: str = "critical") -> dict:
    return {"spec": {"name": name, "severity": severity}, "ok": ok, "summary": ""}


def _round(*, vault_ok: bool = True, minio_ok: bool = True) -> dict:
    return {
        "infra-service": [_result("vault-http", vault_ok)],
        "public-route": [_result("minio-public-route", minio_ok, "warning")],
    }


def test_a_round_counts_one_ok_or_fail_per_probe_and_one_run() -> None:
    ledger = local_ledger.new_ledger("production")
    local_ledger.record_round(ledger, _round(), "production", T0)
    local_ledger.record_round(ledger, _round(minio_ok=False), "production", T0 + 60)

    day = ledger["days"]["2026-09-24"]
    assert day["runs"] == 2
    assert day["signals"]["production:vault-http"] == {
        "ok": 2,
        "fail": 0,
        "severity": "critical",
        "lastDomain": "",
        "lastOkAt": int(T0 + 60),
    }
    assert day["signals"]["production:minio-public-route"] == {
        "ok": 1,
        "fail": 1,
        "severity": "warning",
        "lastDomain": "public-route",
        "lastOkAt": int(T0),
        "lastFailAt": int(T0 + 60),
    }
    assert ledger["updated_at"] == int(T0 + 60)


def _vault_http(ledger: dict, date: str = "2026-09-24") -> dict:
    return ledger["days"][date]["signals"]["production:vault-http"]


def test_a_failed_run_sets_last_fail_at_and_an_ok_run_keeps_it() -> None:
    """#1223: the canary verifier needs the time of the last failure, not only a count."""
    ledger = local_ledger.new_ledger("production")
    local_ledger.record_round(ledger, _round(), "production", T0)
    assert "lastFailAt" not in _vault_http(ledger)

    local_ledger.record_round(ledger, _round(vault_ok=False), "production", T0 + 60)
    local_ledger.record_round(ledger, _round(), "production", T0 + 120)
    entry = _vault_http(ledger)

    assert entry["lastFailAt"] == int(T0 + 60)  # the later ok run did not erase it
    assert entry["lastOkAt"] == int(T0 + 120)  # and the failed run did not erase this
    assert (entry["ok"], entry["fail"]) == (2, 1)

    local_ledger.record_round(ledger, _round(vault_ok=False), "production", T0 + 180)
    entry = _vault_http(ledger)
    assert entry["lastFailAt"] == int(T0 + 180)
    assert entry["lastOkAt"] == int(T0 + 120)
    assert (entry["ok"], entry["fail"]) == (2, 2)


def test_a_result_without_a_verdict_sets_last_fail_at_not_last_ok_at() -> None:
    ledger = local_ledger.new_ledger("staging")
    local_ledger.record_round(ledger, {"g": [{"spec": {"name": "x"}}]}, "staging", T0)

    entry = ledger["days"]["2026-09-24"]["signals"]["staging:x"]
    assert entry["lastFailAt"] == int(T0)
    assert "lastOkAt" not in entry


def test_the_times_are_per_day_and_a_new_day_starts_without_a_last_failure() -> None:
    late = datetime(2026, 9, 24, 23, 59, 30, tzinfo=UTC).timestamp()
    ledger = local_ledger.new_ledger("production")
    local_ledger.record_round(ledger, _round(vault_ok=False), "production", late)
    local_ledger.record_round(ledger, _round(), "production", late + 60)

    first, second = _vault_http(ledger), _vault_http(ledger, "2026-09-25")
    assert first["lastFailAt"] == int(late)
    assert "lastOkAt" not in first
    assert (first["ok"], first["fail"]) == (0, 1)
    assert "lastFailAt" not in second
    assert second["lastOkAt"] == int(late + 60)
    assert (second["ok"], second["fail"]) == (1, 0)


def test_an_ok_run_does_not_invent_a_failure_time_for_an_old_ledger_entry() -> None:
    """A day counted before #1223 holds failures without a time. The writer adds the
    ok time and counts, and it leaves `lastFailAt` out: the reader then fails closed."""
    ledger = local_ledger.new_ledger("production")
    local_ledger.record_round(ledger, _round(), "production", T0)
    entry = _vault_http(ledger)
    entry.pop("lastOkAt")
    entry["fail"] = 2

    local_ledger.record_round(ledger, _round(), "production", T0 + 60)

    assert "lastFailAt" not in entry
    assert entry["lastOkAt"] == int(T0 + 60)
    assert (entry["ok"], entry["fail"]) == (2, 2)


def test_the_times_survive_the_file_and_an_old_file_stays_valid(tmp_path) -> None:
    state = tmp_path / "s.json"
    local_ledger.record_probe_round(_round(vault_ok=False), "production", state, T0)
    local_ledger.record_probe_round(_round(), "production", state, T0 + 60)
    path = tmp_path / local_ledger.LEDGER_FILE_NAME
    entry = _vault_http(json.loads(path.read_text()))
    assert entry["lastFailAt"] == int(T0)
    assert entry["lastOkAt"] == int(T0 + 60)
    assert type(entry["lastFailAt"]) is int and type(entry["lastOkAt"]) is int

    # A file written before #1223 has no times. It still loads and keeps counting.
    old = json.loads(path.read_text())
    for signal in old["days"]["2026-09-24"]["signals"].values():
        signal.pop("lastFailAt", None)
        signal.pop("lastOkAt", None)
    path.write_text(json.dumps(old))
    local_ledger.validate(old)
    local_ledger.record_probe_round(_round(), "production", state, T0 + 120)
    after = json.loads(path.read_text())
    assert _vault_http(after)["ok"] == 2 and _vault_http(after)["fail"] == 1
    assert _vault_http(after)["lastOkAt"] == int(T0 + 120)
    assert "lastFailAt" not in _vault_http(after)
    assert not (tmp_path / "availability-ledger.corrupt.json").exists()


@pytest.mark.parametrize("field", ["lastFailAt", "lastOkAt"])
@pytest.mark.parametrize("bad", ["yesterday", True, -1, 1.5, None])
def test_a_malformed_time_never_costs_the_counts(tmp_path, field, bad) -> None:
    """The writer never reads the times, so a bad one must not make the file corrupt:
    that would drop 21 days of counts. The verifier rejects a bad time itself."""
    state = tmp_path / "s.json"
    local_ledger.record_probe_round(_round(vault_ok=False), "production", state, T0)
    path = tmp_path / local_ledger.LEDGER_FILE_NAME
    document = json.loads(path.read_text())
    _vault_http(document)[field] = bad
    path.write_text(json.dumps(document))

    local_ledger.record_probe_round(_round(), "production", state, T0 + 60)

    entry = _vault_http(json.loads(path.read_text()))
    assert (entry["ok"], entry["fail"]) == (1, 1)
    assert not (tmp_path / "availability-ledger.corrupt.json").exists()


def test_anything_but_ok_true_is_a_failure() -> None:
    """A result without a verdict must never count as a success."""
    ledger = local_ledger.new_ledger("staging")
    local_ledger.record_round(
        ledger,
        {"g": [{"spec": {"name": "x"}}, {"spec": {"name": "x"}, "ok": "yes"}]},
        "staging",
        T0,
    )
    assert ledger["days"]["2026-09-24"]["signals"]["staging:x"]["fail"] == 2


def test_rounds_land_on_their_utc_day() -> None:
    ledger = local_ledger.new_ledger("production")
    for offset in (0, DAY, DAY + 60):
        local_ledger.record_round(ledger, _round(), "production", T0 + offset)
    assert {date: day["runs"] for date, day in ledger["days"].items()} == {
        "2026-09-24": 1,
        "2026-09-25": 2,
    }


def test_prune_keeps_exactly_the_retention_window() -> None:
    ledger = local_ledger.new_ledger("production")
    for back in range(30):
        local_ledger.record_round(ledger, _round(), "production", T0 - back * DAY)
    local_ledger.prune(ledger, T0)
    dates = sorted(ledger["days"])
    assert len(dates) == local_ledger.RETENTION_DAYS == 21
    assert dates[0] == "2026-09-04"
    assert dates[-1] == "2026-09-24"


def test_the_file_is_written_whole_and_readable_by_the_ssh_user(tmp_path) -> None:
    state = tmp_path / "state" / "probe-state.json"
    previous = os.umask(0o077)  # a restrictive umask must not hide it from the SSH user
    try:
        local_ledger.record_probe_round(_round(), "production", state, T0)
        local_ledger.record_probe_round(
            _round(vault_ok=False), "production", state, T0 + 60
        )
    finally:
        os.umask(previous)

    path = state.parent / local_ledger.LEDGER_FILE_NAME
    ledger = json.loads(path.read_text())
    assert ledger["days"]["2026-09-24"]["signals"]["production:vault-http"]["fail"] == 1
    assert ledger["days"]["2026-09-24"]["runs"] == 2
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    # no temporary file is left beside it
    assert [p.name for p in path.parent.iterdir()] == [local_ledger.LEDGER_FILE_NAME]


def test_the_configured_path_wins(tmp_path, monkeypatch) -> None:
    target = tmp_path / "host" / "ledger.json"
    monkeypatch.setenv(local_ledger.LEDGER_FILE_ENV, str(target))
    local_ledger.record_probe_round(_round(), "staging", tmp_path / "s.json", T0)
    assert json.loads(target.read_text())["environment"] == "staging"
    assert not (tmp_path / local_ledger.LEDGER_FILE_NAME).exists()


def test_a_ledger_failure_never_breaks_the_probe_loop(tmp_path, capsys) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    local_ledger.record_probe_round(_round(), "production", blocker / "state.json", T0)
    assert "availability ledger write failed" in capsys.readouterr().out


def test_a_corrupt_ledger_starts_over_and_is_kept_aside(tmp_path, capsys) -> None:
    path = tmp_path / local_ledger.LEDGER_FILE_NAME
    path.write_text("{truncated")
    local_ledger.record_probe_round(_round(), "production", tmp_path / "s.json", T0)
    assert json.loads(path.read_text())["days"]["2026-09-24"]["runs"] == 1
    assert (tmp_path / "availability-ledger.corrupt.json").read_text() == "{truncated"
    assert "was unreadable" in capsys.readouterr().out


def test_host_paths_follow_the_compose_mount() -> None:
    assert local_ledger.host_ledger_path("production") == (
        "/var/lib/infra2-availability-ledger/availability-ledger.json"
    )
    assert local_ledger.host_ledger_path("staging") == (
        "/var/lib/infra2-availability-ledger-staging/availability-ledger.json"
    )
    compose = (ROOT / "platform/12.alerting/compose.yaml").read_text(encoding="utf-8")
    assert (
        "- /var/lib/infra2-availability-ledger${ENV_SUFFIX}:/var/lib/infra2-availability-ledger\n"
        in compose
    )
    assert (
        "INFRA_PROBE_LEDGER_FILE: /var/lib/infra2-availability-ledger/availability-ledger.json"
        in compose
    )


def test_to_report_days_is_the_report_shape_in_date_order() -> None:
    ledger = local_ledger.new_ledger("production")
    local_ledger.record_round(ledger, _round(), "production", T0 + DAY)
    local_ledger.record_round(ledger, _round(), "production", T0)
    days = local_ledger.to_report_days(ledger)
    assert [day["date"] for day in days] == ["2026-09-24", "2026-09-25"]
    assert days[0]["runs"] == 1
    assert set(days[0]["signals"]) == {
        "production:vault-http",
        "production:minio-public-route",
    }


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "infra_probe_runner_ledger_under_test", ROOT / "tools/infra_probe_runner.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_every_probe_round_of_the_runner_lands_in_the_ledger(
    monkeypatch, tmp_path
) -> None:
    """The runner's one call site: raw results, before cascade suppression."""
    runner = _load_runner()
    ledger_file = tmp_path / "host" / "ledger.json"
    monkeypatch.setenv(local_ledger.LEDGER_FILE_ENV, str(ledger_file))
    monkeypatch.setenv("ENV", "staging")
    monkeypatch.delenv("INFRA_PROBE_HEARTBEAT_URL", raising=False)
    monkeypatch.delenv("INFRA_PROBE_DRY_RUN", raising=False)
    monkeypatch.setenv("INFRA_PROBE_SPECS", "vault|http|http://vault|200|critical|5")
    monkeypatch.setenv(
        "PUBLIC_ROUTE_PROBE_SPECS",
        "vault-public-route|http|https://vault.example/v1/sys/health|200|warning|5",
    )
    monkeypatch.setattr(runner, "post_alert_bridge_payload", lambda *_a, **_k: None)

    def fake_run_probes(specs):
        http = (521, "down") if "public-route" in specs[0].name else (200, "ok")
        return [probes.run_probe(specs[0], http_get=lambda *_args: http)]

    monkeypatch.setattr(runner, "run_probes", fake_run_probes)
    for _ in range(3):
        runner.run_once(state_path=tmp_path / "probe-state.json")

    (day,) = json.loads(ledger_file.read_text())["days"].values()
    assert day["runs"] == 3
    assert day["signals"]["staging:vault"]["ok"] == 3
    assert day["signals"]["staging:vault-public-route"]["fail"] == 3


# ---- #911 review: gaps, corruption, durability ------------------------------------

DAY0 = datetime(2026, 9, 24, tzinfo=UTC).timestamp()
SLOT = local_ledger.SLOT_SECONDS


def _runner_day(until: float, *, cadence: float = 66, hole=None, start=DAY0) -> dict:
    """A ledger fed by a runner starting a round every ``cadence`` seconds."""
    ledger = local_ledger.new_ledger("production")
    at = start
    while at < until:
        if not (hole and hole[0] <= at < hole[1]):
            local_ledger.record_round(ledger, _round(), "production", at)
        at += cadence
    return ledger


def test_each_round_marks_its_five_minute_slot_present() -> None:
    ledger = local_ledger.new_ledger("production")
    local_ledger.record_round(ledger, _round(), "production", DAY0 + 7 * SLOT + 12)
    presence = ledger["days"]["2026-09-24"]["presence"]
    assert local_ledger.present_slots(presence) == {7}
    assert len(presence) == local_ledger.SLOTS_PER_DAY // 4 == 72
    assert ledger["started_at"] == int(DAY0 + 7 * SLOT + 12)


def test_a_healthy_runner_leaves_no_gaps() -> None:
    """The loop starts a round every ~66 s; a slow all-timeouts round is ~3 min."""
    for cadence in (66, 180):
        ledger = _runner_day(DAY0 + 86400, cadence=cadence)
        assert local_ledger.gap_intervals(ledger, DAY0 + 86400) == [], cadence


def test_a_45_minute_stop_is_a_45_minute_gap() -> None:
    hole = (DAY0 + 10 * 3600, DAY0 + 10 * 3600 + 45 * 60)
    ledger = _runner_day(DAY0 + 12 * 3600, cadence=60, hole=hole)
    assert local_ledger.gap_intervals(ledger, DAY0 + 12 * 3600) == [hole]


def test_nothing_before_the_first_round_is_a_gap_but_a_missing_day_is() -> None:
    first = DAY0 + 6 * 3600
    ledger = _runner_day(first + 3600, cadence=60, start=first)
    assert local_ledger.gap_intervals(ledger, first + 3600) == []
    # the runner then stopped for the whole next day
    now = DAY0 + 2 * 86400 + 3600
    assert local_ledger.gap_intervals(ledger, now) == [(first + 3600, now)]


def test_the_current_unfinished_slot_is_not_a_gap_yet() -> None:
    ledger = _runner_day(DAY0 + 3600, cadence=60)
    assert local_ledger.gap_intervals(ledger, DAY0 + 3600 + SLOT - 1) == []
    assert local_ledger.gap_intervals(ledger, DAY0 + 3600 + SLOT) == [
        (DAY0 + 3600, DAY0 + 3600 + SLOT)
    ]


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"schema": 2, "days": []},
        {"schema": 1, "started_at": 0, "updated_at": 0, "days": {}},
        {
            "schema": 2,
            "started_at": 0,
            "updated_at": 0,
            "days": {"2026-09-24": ["not", "a", "day"]},
        },
        {
            "schema": 2,
            "started_at": 0,
            "updated_at": 0,
            "days": {
                "2026-09-24": {"runs": 1, "presence": "0" * 72, "signals": {"x": "bad"}}
            },
        },
        {
            "schema": 2,
            "started_at": 0,
            "updated_at": 0,
            "days": {
                "2026-09-24": {
                    "runs": 1,
                    "presence": "0" * 72,
                    "signals": {"x": {"ok": "1", "fail": 0}},
                }
            },
        },
        {
            "schema": 2,
            "started_at": 0,
            "updated_at": 0,
            "days": {"2026-09-24": {"runs": 1, "presence": "zz", "signals": {}}},
        },
        {"schema": 2, "started_at": "yesterday", "updated_at": 0, "days": {}},
    ],
)
def test_a_well_formed_file_of_the_wrong_shape_is_corrupt_not_frozen(
    tmp_path, capsys, document
) -> None:
    """#911 review: valid JSON with wrong types made every round raise, so the
    ledger stopped counting while the loop ran on."""
    path = tmp_path / local_ledger.LEDGER_FILE_NAME
    path.write_text(json.dumps(document))
    local_ledger.record_probe_round(_round(), "production", tmp_path / "s.json", T0)
    ledger = json.loads(path.read_text())
    local_ledger.validate(ledger)
    assert ledger["days"]["2026-09-24"]["runs"] == 1
    assert json.loads((tmp_path / "availability-ledger.corrupt.json").read_text()) == (
        document
    )
    assert "was unreadable" in capsys.readouterr().out


def test_the_data_reaches_the_disk_before_the_rename(tmp_path, monkeypatch) -> None:
    calls: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(
        local_ledger.os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd))[1]
    )
    monkeypatch.setattr(
        local_ledger.os,
        "replace",
        lambda a, b: (calls.append("replace"), real_replace(a, b))[1],
    )
    local_ledger.write_ledger(tmp_path / "l.json", local_ledger.new_ledger("staging"))
    assert calls == ["fsync", "replace"]
