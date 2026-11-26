"""`tools/hook_duration_report.py` warns when a hook p95 passes 70% of its budget (#1823).

The escape: the truealpha `staging_gate` took 117 s, 289 s, then more than 360 s. Its effective
budget is the sum of its check timeouts (360 s), not the 600 s of the cell `staging.post`.
"""

from __future__ import annotations

import json

import pytest
from infra2_sdk.manifests import RuntimeHooksManifest

from tools import hook_duration_report as report

GATE = "truealpha/app:staging_gate"
# The cell budgets of the pinned SDK: the real policy, not a copy.
BUDGETS = report.cell_budgets()


def _staging_manifest(timeouts: tuple[int, ...]) -> RuntimeHooksManifest:
    return RuntimeHooksManifest.from_dict(
        {
            "schema_version": 3,
            "service": "truealpha/app",
            "checks": [
                {
                    "id": f"staging_check_{idx}",
                    "argv": ["sh", "-c", f"echo check {idx}"],
                    "needs": ["running_service", "staging_data"],
                    "writes": False,
                    "accepts_scale": False,
                    "timeout_seconds": timeout,
                }
                for idx, timeout in enumerate(timeouts, start=1)
            ],
        }
    )


MANIFEST_BUDGETS = report.hook_budgets_from_manifests(
    {"truealpha/app": _staging_manifest((90, 30, 240))}
)


def _records(seconds_by_run: list[float], name: str = GATE) -> list[dict]:
    return [
        {
            "env": "staging.release_validation",
            "phase": "post",
            "subject": f"tag:v3.1.{i}",
            "checks": [{"name": name, "ok": True, "skipped": False, "seconds": s}],
        }
        for i, s in enumerate(seconds_by_run)
    ]


def test_the_cell_budget_of_staging_post_is_read_from_the_sdk_policy() -> None:
    assert BUDGETS[("staging.release_validation", "post")] == 600


def test_no_hardcoded_hook_budget_table_remains() -> None:
    assert not hasattr(report, "HOOK_BUDGET_SECONDS")
    assert MANIFEST_BUDGETS[GATE] == 360


def test_load_hook_budgets_derives_budgets_from_fetched_manifests(tmp_path) -> None:
    (tmp_path / "harness").mkdir()
    (tmp_path / "harness" / "repos.yaml").write_text(
        "repositories:\n"
        "  - id: truealpha\n"
        "    source: https://github.com/wangzitian0/truealpha\n"
        "    release_tag: v9.9.9\n",
        encoding="utf-8",
    )
    payload = json.dumps(
        {
            "schema_version": 3,
            "service": "truealpha/app",
            "checks": [
                {
                    "id": "staging_smoke_a",
                    "argv": ["sh", "-c", "echo a"],
                    "needs": ["running_service", "staging_data"],
                    "writes": False,
                    "accepts_scale": False,
                    "timeout_seconds": 90,
                },
                {
                    "id": "staging_smoke_b",
                    "argv": ["sh", "-c", "echo b"],
                    "needs": ["running_service", "staging_data"],
                    "writes": False,
                    "accepts_scale": False,
                    "timeout_seconds": 30,
                },
                {
                    "id": "staging_smoke_c",
                    "argv": ["sh", "-c", "echo c"],
                    "needs": ["running_service", "staging_data"],
                    "writes": False,
                    "accepts_scale": False,
                    "timeout_seconds": 240,
                },
            ],
        }
    )
    loaded = report.load_hook_budgets(
        repo_root=tmp_path, manifest_fetcher=lambda _url: payload
    )
    assert loaded[GATE] == 360


def test_the_117_289_360_history_of_the_staging_gate_is_flagged() -> None:
    history = [117, 119, 115, 117, 121, 289, 291, 288, 295, 365]
    [row] = report.summarize(_records(history), BUDGETS, hook_budgets=MANIFEST_BUDGETS)

    assert row["budget"] == 360
    assert row["p95"] == 365
    assert row["flagged"] is True
    assert report.annotations([row])[0].startswith("::warning title=Hook duration::")


def test_the_289_second_gate_is_flagged_before_it_times_out() -> None:
    [row] = report.summarize(
        _records([117, 118, 117, 289, 291, 290]),
        BUDGETS,
        hook_budgets=MANIFEST_BUDGETS,
    )

    assert row["p95"] == 291
    assert row["flagged"] is True  # 291 s is 81% of 360 s; it is 49% of the cell budget


def test_the_cell_budget_alone_would_have_stayed_quiet() -> None:
    """Mutation: without the hook entry the same history is not flagged."""
    [row] = report.summarize(
        _records([117, 118, 117, 289, 291, 290]), BUDGETS, hook_budgets={}
    )

    assert row["budget"] == 600
    assert row["flagged"] is False


def test_a_healthy_gate_is_not_flagged() -> None:
    [row] = report.summarize(
        _records([115, 117, 119, 116, 118, 117]),
        BUDGETS,
        hook_budgets=MANIFEST_BUDGETS,
    )

    assert row["flagged"] is False


def test_fewer_than_five_samples_are_never_flagged() -> None:
    [row] = report.summarize(
        _records([355, 358, 359, 360]), BUDGETS, hook_budgets=MANIFEST_BUDGETS
    )

    assert row["enough"] is False
    assert row["flagged"] is False
    assert "fewer than 5 samples" in report.render([row])


def test_a_skipped_check_and_a_check_without_seconds_are_not_samples() -> None:
    records = _records([115] * 6)
    records[0]["checks"][0]["skipped"] = True
    records[1]["checks"][0].pop("seconds")

    [row] = report.summarize(records, BUDGETS, hook_budgets=MANIFEST_BUDGETS)

    assert row["samples"] == 4


def test_a_hook_without_any_budget_is_left_out_not_guessed() -> None:
    records = _records([500] * 6, name="other/app:gate")
    for record in records:
        record["env"] = "unknown.stage"

    assert report.summarize(records, BUDGETS) == []


def test_percentile_is_nearest_rank() -> None:
    assert report.percentile(range(1, 101), 0.95) == 95
    assert report.percentile([7], 0.95) == 7
    with pytest.raises(ValueError):
        report.percentile([], 0.95)


def test_main_reports_but_exits_zero_on_a_flagged_hook(monkeypatch, capsys) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    records = _records([117, 118, 117, 289, 291, 290])

    code = report.main(
        ["--repo", "o/r"],
        list_records_fn=lambda *a, **k: records,
        hook_budgets_fn=lambda: MANIFEST_BUDGETS,
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "::warning title=Hook duration::" in out
    assert GATE in out


def test_main_fails_when_the_records_cannot_be_read(monkeypatch, capsys) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "t")

    def broken(*args, **kwargs):
        raise OSError("502")

    assert report.main(["--repo", "o/r"], list_records_fn=broken) == 1
    assert report.main(["--repo", "o/r"], list_records_fn=lambda *a, **k: []) == 1
    assert "cannot read" in capsys.readouterr().out
