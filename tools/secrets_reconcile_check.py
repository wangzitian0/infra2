#!/usr/bin/env python3
"""Scheduled CI wrapper for tools/secrets_reconcile.py (secrets supply chain, PR-E).

The reconcile needs the Vault AppRole and the 1Password service account, which live in
the iac-runner container on the VPS — a GitHub Actions runner has neither. This wrapper
SSHes in with the watchdog key the other ops checks already provision, runs the
reconcile inside the container over its own ``/secrets/.env``, keeps the JSON report for
the alert step, and exits 1 on any finding or transport error. READ-ONLY.

Paging policy (as #531): the step fails on anything, but only a confirmed finding
(store drift or a quota over budget) is page-worthy — never an SSH hiccup.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RUNNER_CONTAINER = "iac-runner"
REMOTE_SCRIPT = (
    "set -a; . /secrets/.env; set +a; "
    "cd /workspace/infra2 && python3 tools/secrets_reconcile.py --json"
)
DEFAULT_REPORT_PATH = "secrets-reconcile-report.json"
PAGING_FINDINGS = ("missing", "empty", "stale", "over_privileged")


def remote_command() -> str:
    return f"docker exec {RUNNER_CONTAINER} sh -c {shlex.quote(REMOTE_SCRIPT)}"


def ssh_args(env: Mapping[str, str]) -> list[str]:
    """The watchdog SSH convention (see libs/vault_self_refresh_audit._ssh)."""
    args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if key_path := env.get("INFRA2_WATCHDOG_SSH_KEY_PATH", "").strip():
        args += [
            "-i",
            key_path,
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
        ]
    if port := env.get("INFRA2_WATCHDOG_SSH_PORT", "").strip():
        args += ["-p", port]
    user = env.get("INFRA2_WATCHDOG_SSH_USER", "").strip() or "root"
    host = (
        env.get("INFRA2_WATCHDOG_SSH_HOST", "").strip()
        or env.get("VPS_HOST", "").strip()
    )
    if not host:
        raise SystemExit("INFRA2_WATCHDOG_SSH_HOST (or VPS_HOST) is required")
    return [*args, f"{user}@{host}", remote_command()]


def run(
    env: Mapping[str, str] | None = None, *, runner=subprocess.run
) -> dict[str, Any]:
    env = os.environ if env is None else env
    result = runner(
        ssh_args(env), text=True, capture_output=True, check=False, timeout=600
    )
    stdout = (result.stdout or "").strip()
    try:
        report = json.loads(stdout[stdout.index("{") :]) if "{" in stdout else None
    except json.JSONDecodeError:
        report = None
    if not isinstance(report, dict):
        return {
            "ok": False,
            "transport_error": (
                f"ssh/docker exec exited {result.returncode}: "
                f"{(result.stderr or stdout or 'no output').strip()[-800:]}"
            ),
        }
    return report


def _page_worthy(report: Mapping[str, Any]) -> list[tuple[tuple[str, ...], str]]:
    """``(identity keys, display text)`` per page-worthy finding: the one place that
    decides what pages, so the summary and the identity cannot disagree (#962).

    A store row's keys are ``store:<service>:<env>:<kind>:<name>`` (names only, as
    the report is); a quota's is ``quota:<name>:<window>`` -- never its reading,
    which changes every day while the finding does not.
    """
    findings: list[tuple[tuple[str, ...], str]] = []
    for row in report.get("stores") or []:
        if row.get("ok"):
            continue
        parts = [f"{k}={row[k]}" for k in PAGING_FINDINGS if row.get(k)]
        if not parts:
            continue
        keys: list[str] = []
        for kind in PAGING_FINDINGS:
            held = row.get(kind)
            if not held:
                continue
            names = held if isinstance(held, (list, tuple, set)) else [""]
            keys += [
                f"store:{row.get('service')}:{row.get('env')}:{kind}:{name}"
                for name in sorted(str(name) for name in names)
            ]
        findings.append(
            (
                tuple(keys),
                f"- {row.get('service')} {row.get('env')}: {', '.join(parts)}",
            )
        )
    trend = report.get("capacity_trend") or {}
    for item in (report.get("capacity") or {}).get("items") or []:
        if item.get("level") == "exceeded":
            text = (
                f"- quota {item.get('name')} {item.get('used')}/{item.get('limit')} "
                f"per {item.get('window')} exceeded"
            )
            # The trend (rendered inside the runner, where the SDK lives) tells a
            # one-day spike from a budget that has been creeping up.
            if trend.get("name") == item.get("name") and trend.get("rendered"):
                text += f"\n  {trend['rendered']}"
            findings.append(((f"quota:{item.get('name')}:{item.get('window')}",), text))
    return findings


def page_worthy_summary(report: Mapping[str, Any]) -> str:
    """Findings worth a page, or '' (transport errors, warn-level quotas and
    ``unclassified`` leftovers are not: a key nobody declared cannot break a deploy; it
    stays in the run log for cleanup, see #649). ``over_privileged`` always pages: an
    application holding the object store's root credential is #677 happening again.
    """
    return "\n".join(text for _keys, text in _page_worthy(report))


def page_worthy_keys(report: Mapping[str, Any]) -> list[str]:
    """The identity keys of the page-worthy findings (#962): what the cross-run page
    dedup compares. Empty exactly when ``page_worthy_summary`` is."""
    return [key for keys, _text in _page_worthy(report) for key in keys]


def unevaluated_prefixes(report: Mapping[str, Any]) -> list[str]:
    """Key prefixes of what this report did not observe (#962).

    A paged finding under one of them has not recovered, it is unknown: no report
    at all (transport error) is everything (``""``); an unread quota section is
    every `quota:` finding; a store reconciled while 1Password was unavailable (its
    ``note``) cannot prove its own `stale` / `missing` / `empty` findings.
    """
    if report.get("transport_error") or not isinstance(report.get("stores"), list):
        return [""]
    prefixes: list[str] = []
    if not isinstance(report.get("capacity"), Mapping):
        prefixes.append("quota:")
    prefixes += [
        f"store:{row.get('service')}:{row.get('env')}:"
        for row in report["stores"]
        if row.get("note")
    ]
    return prefixes


def report_path(env: Mapping[str, str]) -> Path:
    return Path(env.get("SECRETS_RECONCILE_REPORT") or DEFAULT_REPORT_PATH)


def load_report(env: Mapping[str, str]) -> dict[str, Any]:
    path = report_path(env)
    if not path.exists():
        return {"ok": False, "transport_error": f"no report at {path}"}
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    report = run()
    report_path(os.environ).write_text(json.dumps(report, indent=1), encoding="utf-8")
    if error := report.get("transport_error"):
        print(f"secrets reconcile could not run: {error}")
        return 1
    print(report.get("rendered") or json.dumps(report, indent=1))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
