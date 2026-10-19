"""#658: the two scheduled jobs that stayed silent this week now page.

The weekly digest died on the watchdog ledger two Mondays running with no failure path;
the runner's /health carried a dead 1Password service account for three days with no
scheduled reader. Both alert paths deliver through deliver_out_of_band_alert.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ops-checks.yml"


def _steps(job: dict) -> dict[str, dict]:
    return {step.get("name"): step for step in job["steps"] if step.get("name")}


def test_the_weekly_digest_pages_when_it_fails() -> None:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    digest = jobs["digest"]
    steps = _steps(digest)
    alert = steps["Alert on failure"]
    names = [step.get("name") for step in digest["steps"]]
    assert names.index("Build and send positive stability report") < names.index(
        "Alert on failure"
    )
    assert (
        alert["if"].startswith("failure()")
        and "INFRA2_STABILITY_REPORT_DRY_RUN" in alert["if"]
    )
    assert "deliver_out_of_band_alert" in alert["run"]
    assert (
        digest["env"]["INFRA2_OUT_OF_BAND_FEISHU_CHAT_ID"]
        == "${{ secrets.INFRA2_OUT_OF_BAND_FEISHU_CHAT_ID }}"
    )


def test_the_nightly_watchdog_reads_the_runner_health_and_pages_on_a_dead_prerequisite() -> (
    None
):
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    watchdog = jobs["watchdog"]
    steps = _steps(watchdog)
    health = steps["Runner health — 1Password service account and deploy prerequisites"]
    assert health["if"] == "always()"
    assert "/health" in health["run"] and "op_service_account_token" in health["run"]
    assert (
        "deliver_out_of_band_alert" in health["run"] and "sys.exit(1)" in health["run"]
    )
    assert health["env"]["IAC_RUNNER_URL"].startswith("${{ vars.IAC_RUNNER_URL")
