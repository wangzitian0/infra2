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
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from infra2_sdk.observability import CheckVerdict
from common.core._sdk_wire import parse_iso_utc, reject_unknown
from common.core.ci_contract import load_delivery_stages


_SUBJECT_KINDS = ("sha", "tag", "pr", "service")
_UNIT_KINDS = ("app", "sdk", "infra", "harness")
_KEYS = {"schema", "verdict"}


def _kinded(items: Any, kinds: tuple[str, ...], name: str) -> tuple[str, ...]:
    for item in items:
        kind, _, ident = item.partition(":") if isinstance(item, str) else ("", "", "")
        if kind not in kinds or not ident:
            raise ValueError(f"{name} {item!r} must be <kind>:<id>, kind in {kinds}")
    return tuple(items)


@dataclass(frozen=True, kw_only=True)
class StageRecord:
    env: str
    phase: str
    subject: str
    ref: str
    event: str
    run_url: str
    started_at: str
    finished_at: str
    checks: tuple[CheckVerdict, ...]
    units: tuple[str, ...] = ()
    uncovered: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.env not in load_delivery_stages():
            raise ValueError(f"unknown env {self.env!r}")
        _kinded([self.subject], _SUBJECT_KINDS, "subject")
        if self.phase not in ("pre", "operation", "post"):
            raise ValueError(f"unknown phase {self.phase!r}")
        if self.event not in ("push", "tag", "dispatch", "schedule", "pull_request"):
            raise ValueError(f"unknown event {self.event!r}")
        if not (self.ref and self.run_url and self.checks):
            raise ValueError("ref, run_url and checks must not be empty")
        if parse_iso_utc(self.finished_at) < parse_iso_utc(self.started_at):
            raise ValueError("finished_at is before started_at")
        if not all(
            isinstance(c.name, str)
            and c.name
            and type(c.ok) is bool
            and type(c.skipped) is bool
            and not (c.skipped and c.ok)
            and type(c.seconds) in (int, float)
            and c.seconds >= 0
            for c in self.checks
        ):
            raise ValueError("a check needs a name, a bool ok and seconds >= 0")
        if len({c.name for c in self.checks}) != len(self.checks):
            raise ValueError("check ids must be unique")
        object.__setattr__(self, "checks", tuple(self.checks))
        object.__setattr__(self, "units", _kinded(self.units, _UNIT_KINDS, "units"))
        object.__setattr__(
            self, "uncovered", _kinded(self.uncovered, _UNIT_KINDS, "uncovered")
        )

    @property
    def verdict(self) -> str:
        active = [c for c in self.checks if not c.skipped]
        return "pass" if active and all(c.ok is True for c in active) else "fail"

    def to_dict(self) -> dict[str, Any]:
        return {"schema": 1, "verdict": self.verdict, **asdict(self)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StageRecord:
        if not isinstance(data, Mapping):
            raise ValueError("stage record must be a JSON object")
        reject_unknown(data, {f.name for f in fields(cls)} | _KEYS, "stage record")
        if data.get("schema", 1) != 1:
            raise ValueError("unsupported stage record schema")
        raw = data.get("checks")
        if not isinstance(raw, (list, tuple)) or not all(
            isinstance(c, Mapping) for c in raw
        ):
            raise ValueError("checks must be a list of objects")
        body = {k: v for k, v in data.items() if k not in _KEYS}
        try:
            rec = cls(**{**body, "checks": tuple(CheckVerdict(**c) for c in raw)})
        except TypeError as err:
            raise ValueError(str(err)) from err
        if data.get("verdict", rec.verdict) != rec.verdict:
            raise ValueError(f"verdict does not match checks: {rec.verdict}")
        return rec


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
