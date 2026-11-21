"""Send the ops-checks scheduler heartbeat and record its verdict (#1448, #1877).

The Cloudflare Worker pages when this daily ping is older than its limit. A send
that fails must leave a verdict for the issue trail: a missing token made the
step red for three nights (2026-11-19 to 21) and opened no issue.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping

from infra2_sdk.observability import CheckVerdict, record_verdicts

HEARTBEAT_SOURCE = "ops-checks-scheduler-heartbeat"
DEFAULT_URL = "https://infra2-cloudflare-watchdog.wangzitian-ai.workers.dev/heartbeat"
VERDICTS_ENV = "INFRA2_WATCHDOG_VERDICTS_PATH"
ATTEMPTS = 3


def heartbeat_url(env: Mapping[str, str]) -> str:
    url = (env.get("INFRA2_WATCHDOG_HEARTBEAT_URL") or "").strip()
    if url:
        return url
    status_url = (env.get("INFRA2_WATCHDOG_WORKER_STATUS_URL") or "").strip()
    if status_url.endswith("/status"):
        return status_url[: -len("/status")] + "/heartbeat"
    return DEFAULT_URL


def send(
    env: Mapping[str, str],
    *,
    opener: Callable[..., object] = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> CheckVerdict:
    """Return the verdict of one heartbeat send. It never raises."""
    token = (env.get("INFRA2_WATCHDOG_HEARTBEAT_TOKEN") or "").strip()
    if not token:
        return _red(
            "INFRA2_WATCHDOG_HEARTBEAT_TOKEN is missing; the heartbeat was not sent"
        )
    payload = json.dumps(
        {
            "env": "production",
            "name": "ops-checks-scheduler",
            "ok": True,
            "detail": "ops-checks watchdog scheduler tick",
            "timestamp": int(now()),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        heartbeat_url(env),
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "infra2-ops-checks/1.0",
        },
        method="POST",
    )
    last_error = ""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with opener(request, timeout=15) as response:  # type: ignore[attr-defined]
                body = response.read(512).decode("utf-8", errors="replace")
                if 200 <= response.status < 300:
                    return CheckVerdict(
                        HEARTBEAT_SOURCE, True, f"HTTP {response.status}: {body}"
                    )
                last_error = f"HTTP {response.status}: {body}"
        except (urllib.error.URLError, OSError) as exc:
            last_error = str(exc)
        if attempt < ATTEMPTS:
            sleep(5)
    return _red(f"the heartbeat failed after {ATTEMPTS} attempts: {last_error}")


def _red(detail: str) -> CheckVerdict:
    return CheckVerdict(HEARTBEAT_SOURCE, False, detail, "P1", "ops-scheduler")


def main(env: Mapping[str, str] | None = None, **send_kwargs: object) -> int:
    current = os.environ if env is None else env
    verdict = send(current, **send_kwargs)  # type: ignore[arg-type]
    path = (current.get(VERDICTS_ENV) or "").strip()
    if path:
        try:
            record_verdicts(path, source=HEARTBEAT_SOURCE, checks=[verdict])
        except OSError as error:
            print(f"could not record the heartbeat verdict: {error}", file=sys.stderr)
    if verdict.ok:
        print(f"ops-checks scheduler heartbeat sent ({verdict.detail})")
        return 0
    print(f"::error::{verdict.detail}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
