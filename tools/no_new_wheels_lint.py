#!/usr/bin/env python3
"""No-new-wheels lint (#542 task 5): every alert-delivery surface must be registered.

The convergence work (#531/#475/#425/#541/#542) repeatedly found the same
failure shape: someone adds a NEW check/watcher/cron with its own hand-rolled
alerting, outside the signal registry, and it drifts unnoticed. This lint makes
that structurally impossible going forward:

1. Every Python module under ``tools/``/``libs/`` that CALLS an alert-delivery
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

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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
    "libs/alerting.py",  # defines deliver_infra2_report + the shared engine
    "libs/infra_probes.py",  # defines post_alert_bridge_payload
    "tools/no_new_wheels_lint.py",
}

ALERTS_AS_RE = re.compile(r"#\s*alerts-as:\s*([A-Za-z0-9._-]+)")
DELIVERY_EXEMPT_RE = re.compile(r"#\s*alert-delivery-exempt:\s*(\S.*)")
JOB_SIGNAL_RE = re.compile(r"#\s*signal:\s*([A-Za-z0-9._-]+)")
JOB_EXEMPT_RE = re.compile(r"#\s*schedule-signal-exempt:\s*(\S.*)")


def registered_signal_names() -> set[str]:
    import yaml

    data = yaml.safe_load(SIGNALS_YAML.read_text(encoding="utf-8"))
    return {str(s.get("signal", "")) for s in data.get("signals", [])}


def lint_python_callsites(
    root: Path = ROOT, signals: set[str] | None = None
) -> list[str]:
    signals = signals if signals is not None else registered_signal_names()
    errors: list[str] = []
    for folder in ("tools", "libs"):
        for path in sorted((root / folder).glob("*.py")):
            rel = path.relative_to(root).as_posix()
            if rel in SCAN_EXEMPT_FILES:
                continue
            text = path.read_text(encoding="utf-8")
            if not any(p in text for p in PRIMITIVES):
                continue
            exempt = DELIVERY_EXEMPT_RE.search(text)
            if exempt:
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
    ops_checks: Path = OPS_CHECKS, signals: set[str] | None = None
) -> list[str]:
    signals = signals if signals is not None else registered_signal_names()
    text = ops_checks.read_text(encoding="utf-8")
    # only real job ids (yaml-parsed) count as headers — `on:`'s nested keys
    # (workflow_dispatch etc.) are 2-space-indented too and must not match
    import yaml

    job_ids = set((yaml.safe_load(text).get("jobs") or {}).keys())
    jobs_start = text.index("\njobs:")
    job_re = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$", re.M)
    headers = [
        m
        for m in job_re.finditer(text, jobs_start)
        if m.group(1) in job_ids
    ]
    errors: list[str] = []
    for i, m in enumerate(headers):
        start = m.start()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        block = text[start:end]
        if "github.event.schedule" not in block:
            continue  # not a scheduled job
        if JOB_EXEMPT_RE.search(block):
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


def main() -> int:
    signals = registered_signal_names()
    errors = lint_python_callsites(signals=signals) + lint_scheduled_jobs(
        signals=signals
    )
    for e in errors:
        print(f"ERROR: {e}")
    if errors:
        return 1
    print("no-new-wheels lint passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
