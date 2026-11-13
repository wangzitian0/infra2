"""The one writer of stage records (#1645).

Every stage task writes one ``infra2_sdk.stage_record.StageRecord`` after each phase
(``pre``, ``operation``, ``post``). The reconcile run (``tools.reconcile_iac_inputs``) and the
``record`` job of ``infra-ci.yml`` call ``write_stage_record`` and no other code builds a
record. A workflow step uploads each file as the artifact ``stage-<env>-<phase>-<subject>``;
``tools/stage_records.py`` (the reader, ``common/deploy/stage_record_reader.py``) reads them back.

Running the module writes the post record of one commit on main from the ``needs``
context of the ``record`` job.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from infra2_sdk.observability import CheckVerdict
from infra2_sdk.stage_record import StageRecord

RECORD_DIR = "stage-records"
CI_ENV = "github_ci.merge_authority"
#: A job result that counts as green. GitHub counts a skipped required job as passed.
GREEN_RESULTS = frozenset({"success", "skipped"})


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def artifact_name(env: str, phase: str, subject: str) -> str:
    """``stage-<env>-<phase>-<subject>``. An artifact name cannot hold a colon."""
    return f"stage-{env}-{phase}-{subject.replace(':', '-')}"


def sdk_unit() -> str:
    """The unit ``sdk:<version>`` of the infra2-sdk that runs. CI installs the wheel that
    ``tools/sdk_requirement.py`` reads from ``uv.lock``, and that script is the one reader of the lock.
    It never raises: a missing package gives ``sdk:unknown`` and a warning."""
    try:
        return f"sdk:{version('infra2-sdk')}"
    except PackageNotFoundError:
        print("::warning::infra2-sdk is not installed; the SDK unit is sdk:unknown")
        return "sdk:unknown"


def write_stage_record(
    *,
    env: str,
    phase: str,
    subject: str,
    ref: str,
    event: str,
    run_url: str,
    started_at: str,
    checks: Sequence[CheckVerdict],
    units: Iterable[str] = (),
    uncovered: Iterable[str] = (),
    directory: str | Path | None = None,
    persist: bool = True,
) -> StageRecord:
    """Build and validate one record; write it to ``<directory>/<artifact name>.json``.
    The directory is ``directory``, else ``$STAGE_RECORD_DIR``, else ``stage-records``.

    ``persist=False`` (a dry run) only validates. The step variable
    ``STAGE_RECORD_<PHASE>`` carries the artifact name to the upload step of the job.
    """
    record = StageRecord(
        env=env,
        phase=phase,
        subject=subject,
        ref=ref,
        event=event,
        run_url=run_url,
        started_at=started_at,
        finished_at=utc_now(),
        checks=tuple(checks),
        units=tuple(units),
        uncovered=tuple(uncovered),
    )
    if persist:
        name = artifact_name(env, phase, subject)
        target = Path(directory or os.environ.get("STAGE_RECORD_DIR", RECORD_DIR))
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{name}.json").write_text(
            json.dumps(record.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
        if github_env := os.environ.get("GITHUB_ENV"):
            with open(github_env, "a", encoding="utf-8") as handle:
                handle.write(f"STAGE_RECORD_{phase.upper()}={name}\n")
    return record


def ci_checks(needs: Mapping[str, Mapping[str, object]]) -> list[CheckVerdict]:
    """One check for each job in the ``needs`` context. Check id = job id."""
    return [
        CheckVerdict(
            name=job,
            ok=str(info.get("result")) in GREEN_RESULTS,
            detail=str(info.get("result")),
            seconds=0.0,
        )
        for job, info in sorted(needs.items())
    ]


def ci_post_record(env: Mapping[str, str], directory: str | Path | None = None):
    """The post record of the commit on main that ``env`` names. None when a job was cancelled.

    A cancelled job means a newer push replaced this run, so the run proves nothing.
    """
    needs = json.loads(env["NEEDS_JSON"])
    if any(str(info.get("result")) == "cancelled" for info in needs.values()):
        print("::notice::a needed job was cancelled; no CI record for this run")
        return None
    started = utc_now()
    return write_stage_record(
        env=CI_ENV,
        phase="post",
        subject=f"sha:{env['GITHUB_SHA']}",
        ref=env["GITHUB_REF"],
        event="push",
        run_url=f"{env['GITHUB_SERVER_URL']}/{env['GITHUB_REPOSITORY']}/actions/runs/{env['GITHUB_RUN_ID']}",
        started_at=started,
        checks=ci_checks(needs),
        units=(sdk_unit(),),
        directory=directory,
    )


if __name__ == "__main__":
    ci_post_record(os.environ)
