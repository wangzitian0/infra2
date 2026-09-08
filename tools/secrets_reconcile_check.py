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
PAGING_FINDINGS = ("missing", "empty", "stale")


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


def page_worthy_summary(report: Mapping[str, Any]) -> str:
    """Findings worth a page, or '' (transport errors, warn-level quotas and
    ``unclassified`` leftovers are not: a key nobody declared cannot break a deploy; it
    stays in the run log for cleanup, see #649)."""
    lines: list[str] = []
    for row in report.get("stores") or []:
        if row.get("ok"):
            continue
        parts = [f"{k}={row[k]}" for k in PAGING_FINDINGS if row.get(k)]
        if parts:
            lines.append(f"- {row.get('service')} {row.get('env')}: {', '.join(parts)}")
    for item in (report.get("capacity") or {}).get("items") or []:
        if item.get("level") == "exceeded":
            lines.append(
                f"- quota {item.get('name')} {item.get('used')}/{item.get('limit')} "
                f"per {item.get('window')} exceeded"
            )
    return "\n".join(lines)


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
