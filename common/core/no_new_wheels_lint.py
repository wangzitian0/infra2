#!/usr/bin/env python3
"""No-new-wheels lint (#542 task 5): every alert-delivery surface must be registered.

The convergence work (#531/#475/#425/#541/#542) repeatedly found the same
failure shape: someone adds a NEW check/watcher/cron with its own hand-rolled
alerting, outside the signal registry, and it drifts unnoticed. This lint makes
that structurally impossible going forward:

1. Every Python module under ``tools/``/``common/`` that CALLS an alert-delivery
   primitive (``deliver_out_of_band_alert`` / ``post_alert_bridge_payload`` /
   ``deliver_infra2_report``) must carry either
   ``# alerts-as: <signal>`` (a ``signal:`` value registered in
   ``docs/ssot/watchdog-signals.yaml``) or
   ``# alert-delivery-exempt: <reason>`` (explicit, greppable, reviewed).
2. Every SCHEDULED job in ``.github/workflows/ops-checks.yml`` (gated on
   ``github.event.schedule``) must carry, inside its job block, a
   ``# signal: <signal>`` annotation (registered) or
   ``# schedule-signal-exempt: <reason>``.

Adding a new alerting wheel without registering it in the one signal registry
now fails CI with a message pointing at the facet/signal model.
"""

from __future__ import annotations

import datetime
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SIGNALS_YAML = ROOT / "docs/ssot/watchdog-signals.yaml"
OPS_CHECKS = ROOT / ".github/workflows/ops-checks.yml"
PRIMITIVES = (
    "deliver_out_of_band_alert(",
    "post_alert_bridge_payload(",
    "deliver_infra2_report(",
)
# The primitives' own defining modules and the shared engine are the plumbing,
# not callsites; tests exercise callsites by design.
SCAN_EXEMPT_FILES = {
    "tools/out_of_band_watchdog.py",  # defines deliver_out_of_band_alert
    "common/alerting/delivery.py",  # defines deliver_infra2_report + the shared engine
    "common/observability/probes.py",  # defines post_alert_bridge_payload
    "common/core/no_new_wheels_lint.py",
}

ALERTS_AS_RE = re.compile(r"#\s*alerts-as:\s*([A-Za-z0-9._-]+)")
DELIVERY_EXEMPT_RE = re.compile(r"#\s*(?:alert-delivery-exempt|nowheel):\s*(\S.*)")
JOB_SIGNAL_RE = re.compile(r"#\s*signal:\s*([A-Za-z0-9._-]+)")
JOB_EXEMPT_RE = re.compile(r"#\s*(?:schedule-signal-exempt|nowheel):\s*(\S.*)")
REVIEW_DATE_RE = re.compile(r"(?:^|;\s*)review=(\d{4}-\d{2}-\d{2})\b")


def _check_exemption_expiry(
    target: str,
    annotation_text: str,
    today: datetime.date,
    *,
    require_review_date: bool = True,
) -> str | None:
    match = REVIEW_DATE_RE.search(annotation_text)
    if match is None:
        if require_review_date:
            return (
                f"{target}: exemption annotation must declare "
                "'; review=YYYY-MM-DD' (#720 debt expiration audit)"
            )
        return None
    try:
        review_date = datetime.date.fromisoformat(match.group(1))
    except ValueError:
        return (
            f"{target}: invalid review date '{match.group(1)}' in exemption annotation"
        )
    if review_date < today:
        return (
            f"{target}: exemption expired on {review_date.isoformat()} "
            f"(today is {today.isoformat()}) — renew review date or register a signal (#720)"
        )
    return None


def _load_signals(signals_yaml: Path = SIGNALS_YAML) -> list[dict]:
    import yaml

    data = yaml.safe_load(signals_yaml.read_text(encoding="utf-8")) or {}
    return [s for s in data.get("signals", []) if isinstance(s, dict)]


def registered_signal_names() -> set[str]:
    return {str(s.get("signal", "")) for s in _load_signals()}


def day_report_self_signals(signals_yaml: Path = SIGNALS_YAML) -> set[str]:
    return {
        str(s.get("signal", ""))
        for s in _load_signals(signals_yaml)
        if s.get("primary_owner") == "self"
        and s.get("tier") == "day"
        and s.get("type") == "report"
    }


def lint_python_callsites(
    root: Path = ROOT,
    signals: set[str] | None = None,
    *,
    today: datetime.date | None = None,
) -> list[str]:
    signals = signals if signals is not None else registered_signal_names()
    current_date = today if today is not None else datetime.date.today()
    errors: list[str] = []
    for folder in ("tools", "common"):
        # rglob, not glob: the #846 domain packages (common/observability/, common/deploy/,
        # ...) hold implementations that a flat-only scan would stop covering the moment
        # a file moves into one -- silently, with the lint still green.
        for path in sorted((root / folder).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if rel in SCAN_EXEMPT_FILES or "/tests/" in rel:
                continue
            text = path.read_text(encoding="utf-8")
            if not any(p in text for p in PRIMITIVES):
                continue
            exempt = DELIVERY_EXEMPT_RE.search(text)
            if exempt:
                err = _check_exemption_expiry(
                    rel, exempt.group(1), current_date, require_review_date=True
                )
                if err is not None:
                    errors.append(err)
                continue
            declared = ALERTS_AS_RE.findall(text)
            if not declared:
                errors.append(
                    f"{rel}: calls an alert-delivery primitive but declares no "
                    "'# alerts-as: <signal>' (and no '# alert-delivery-exempt: "
                    "<reason>') — register the signal in watchdog-signals.yaml "
                    "(#542 no-new-wheels)"
                )
                continue
            for name in declared:
                if name not in signals:
                    errors.append(
                        f"{rel}: '# alerts-as: {name}' does not match any "
                        f"registered signal in {SIGNALS_YAML.name}"
                    )
    return errors


def lint_scheduled_jobs(
    ops_checks: Path = OPS_CHECKS,
    signals: set[str] | None = None,
    *,
    today: datetime.date | None = None,
) -> list[str]:
    signals = signals if signals is not None else registered_signal_names()
    current_date = today if today is not None else datetime.date.today()
    text = ops_checks.read_text(encoding="utf-8")
    # only real job ids (yaml-parsed) count as headers — `on:`'s nested keys
    # (workflow_dispatch etc.) are 2-space-indented too and must not match
    import yaml

    job_ids = set((yaml.safe_load(text).get("jobs") or {}).keys())
    jobs_start = text.index("\njobs:")
    job_re = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$", re.M)
    headers = [m for m in job_re.finditer(text, jobs_start) if m.group(1) in job_ids]
    errors: list[str] = []
    for i, m in enumerate(headers):
        start = m.start()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        block = text[start:end]
        if "github.event.schedule" not in block:
            continue  # not a scheduled job
        exempt = JOB_EXEMPT_RE.search(block)
        if exempt:
            err = _check_exemption_expiry(
                f"ops-checks.yml job '{m.group(1)}'",
                exempt.group(1),
                current_date,
                require_review_date="# nowheel:" in exempt.group(0),
            )
            if err is not None:
                errors.append(err)
            continue
        declared = JOB_SIGNAL_RE.findall(block)
        if not declared:
            errors.append(
                f"ops-checks.yml job '{m.group(1)}' is schedule-gated but "
                "declares no '# signal: <signal>' (and no "
                "'# schedule-signal-exempt: <reason>') — #542 no-new-wheels"
            )
            continue
        for name in declared:
            if name not in signals:
                errors.append(
                    f"ops-checks.yml job '{m.group(1)}': '# signal: {name}' is "
                    "not a registered signal"
                )
    return errors


def lint_workflow_callsites(
    workflows_dir: Path | None = None,
    signals: set[str] | None = None,
    *,
    day_report_signals: set[str] | None = None,
    today: datetime.date | None = None,
) -> list[str]:
    import yaml

    wf_dir = (
        workflows_dir if workflows_dir is not None else (ROOT / ".github/workflows")
    )
    signals = signals if signals is not None else registered_signal_names()
    day_reports = (
        day_report_signals
        if day_report_signals is not None
        else day_report_self_signals()
    )
    current_date = today if today is not None else datetime.date.today()
    errors: list[str] = []
    if not wf_dir.is_dir():
        return errors
    for path in sorted(wf_dir.glob("*.y*ml")):
        text = path.read_text(encoding="utf-8")
        if "\njobs:" not in text:
            continue
        job_ids = set(((yaml.safe_load(text) or {}).get("jobs") or {}).keys())
        jobs_start = text.index("\njobs:")
        job_re = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$", re.M)
        headers = [
            m for m in job_re.finditer(text, jobs_start) if m.group(1) in job_ids
        ]
        for i, m in enumerate(headers):
            job_id = m.group(1)
            start = m.start()
            end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
            block = text[start:end]
            uses_primitive = any(p in block for p in PRIMITIVES) or (
                "deliver_page=deliver_out_of_band_alert" in block
            )
            if not uses_primitive:
                continue
            target = f"{path.name} job '{job_id}'"
            delivery_exempt = DELIVERY_EXEMPT_RE.search(block)
            if delivery_exempt:
                err = _check_exemption_expiry(
                    target,
                    delivery_exempt.group(1),
                    current_date,
                    require_review_date=True,
                )
                if err is not None:
                    errors.append(err)
                continue
            sched_exempt = JOB_EXEMPT_RE.search(block)
            if sched_exempt:
                err = _check_exemption_expiry(
                    target,
                    sched_exempt.group(1),
                    current_date,
                    require_review_date="# nowheel:" in sched_exempt.group(0),
                )
                if err is not None:
                    errors.append(err)
                continue
            declared = JOB_SIGNAL_RE.findall(block)
            if not declared:
                errors.append(
                    f"{target}: calls an alert-delivery primitive in inline workflow "
                    "Python without '# signal: <signal>' or '# alert-delivery-exempt: "
                    "<reason>; review=YYYY-MM-DD' (#1590 no-new-wheels)"
                )
                continue
            for name in declared:
                if name not in signals:
                    errors.append(
                        f"{target}: '# signal: {name}' is not a registered signal"
                    )
                elif name in day_reports:
                    if (
                        "deliver_out_of_band_alert(" in block
                        or "dedup_page(" not in block
                    ):
                        errors.append(
                            f"{target}: day/report signal '{name}' must route paging "
                            "through common.observability.page_dedup.dedup_page "
                            "(direct deliver_out_of_band_alert call forbidden; #1590)"
                        )
    return errors


def main() -> int:
    signals = registered_signal_names()
    errors = (
        lint_python_callsites(signals=signals)
        + lint_scheduled_jobs(signals=signals)
        + lint_workflow_callsites(signals=signals)
    )
    for e in errors:
        print(f"ERROR: {e}")
    if errors:
        return 1
    print("no-new-wheels lint passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
