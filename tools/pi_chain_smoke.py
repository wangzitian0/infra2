#!/usr/bin/env python3
"""Prove the pi main chain end-to-end with a real token-consuming model call.

The owner's architecture verdict (Infra-019, 2026-09-21): pi is THE main-chain
host — if the pi chain works, the harness mechanism works, and mechanism
success must be proven by real runs that consume tokens. Component tests and
read-only observation proved the map, not the territory: the chain was broken
in production (direnv activation dead, default-model resolution falling back
to an unconfigured Bedrock route with invalid credentials, broken skill YAML)
while every unit/fixture gate stayed green, because no test ever launched the
real chain.

This smoke launches a fresh pi CLI process against zai-coding-cn /
glm-5.3-flash in non-interactive JSON mode and asserts the full chain:

- process exit 0 and a complete agent_end event;
- provider routing (zai-coding-cn / glm-5.3-flash — a bare `pi -p` silently
  falls back to other providers, so the route is asserted explicitly);
- stopReason == "stop" (no error/abort/length);
- the sentinel text is echoed (prompt comprehension, not just transport);
- 0 < totalTokens <= budget (tokens really consumed; the ceiling also catches
  system-prompt bloat regressions before they hit the bill).

Scheduled observational check only: it consumes external model quota and
depends on a remote model's prose, so it must never gate merges and delivers
no alerts (# schedule-signal-exempt in ops-checks.yml).

Exit contract (mirrors tools/omca_gate_policy.py):
  0  pass — or SKIP when no credentials are available (pass --strict in CI to
     turn a missing credential into a hard infra error instead);
  1  chain failure (an assertion about the real run did not hold);
  2  infra error (pi binary missing, timeout, unparseable output).

Usage:
  python3 tools/pi_chain_smoke.py                 # local: SKIP without creds
  python3 tools/pi_chain_smoke.py --strict        # CI: missing creds -> exit 2
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROVIDER = "zai-coding-cn"
MODEL = "glm-5.3-flash"
PROMPT = "Reply with exactly: CHAIN-OK"
SENTINEL = "CHAIN-OK"
BUDGET_TOTAL_TOKENS = 30_000
TIMEOUT_S = 180
MAX_ATTEMPTS = 2
BACKOFF_S = 5


def preflight(strict: bool) -> tuple[str, str]:
    """Return (status, detail): "ok", "skip", or "infra" for missing pieces."""
    if not shutil.which("pi"):
        return "infra", "pi binary not found on PATH"
    if os.environ.get("ZAI_CODING_CN_API_KEY"):
        return "ok", "credential from ZAI_CODING_CN_API_KEY env"
    agent_dir = Path(os.environ.get("PI_CODING_AGENT_DIR", "~/.pi/agent"))
    auth = agent_dir.expanduser() / "auth.json"
    if auth.is_file():
        try:
            keys = json.loads(auth.read_text())
            if isinstance(keys, dict) and PROVIDER in keys:
                return "ok", f"credential from {auth}"
        except (OSError, ValueError):
            pass
        return ("infra", "no zai-coding-cn credential in env or auth.json") \
            if strict else ("skip", f"no {PROVIDER} credential (env/auth.json)")
    return ("infra", "no zai-coding-cn credential in env or auth.json") if strict \
        else ("skip", f"no {PROVIDER} credential (env/auth.json)")


def run_once() -> tuple[dict | None, str]:
    """Run pi once; return (assertions, error). assertions=None means transport-
    class failure (timeout / crash before a usable event stream)."""
    cmd = ["pi", "--provider", PROVIDER, "--model", MODEL, "--mode", "json",
           "--no-session", "-p", PROMPT]
    env = dict(os.environ, PI_SKIP_VERSION_CHECK="1")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired:
        return None, f"timeout after {TIMEOUT_S}s"
    events: list[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue  # tolerate unknown/non-NDJSON noise, anchor on typed events
    ends = [e for e in events
            if e.get("type") == "message_end"
            and isinstance(e.get("message"), dict)
            and e["message"].get("role") == "assistant"]
    if not ends:
        return None, (f"no assistant message_end event (exit={proc.returncode}, "
                      f"stderr={proc.stderr.strip()[:200]!r})")
    message = ends[-1]["message"]
    usage = message.get("usage") or {}
    text = "".join(c.get("text", "") for c in message.get("content", [])
                   if isinstance(c, dict) and c.get("type") == "text")
    total = usage.get("totalTokens")
    checks = {
        "exit0": proc.returncode == 0,
        "agent_end": any(e.get("type") == "agent_end" for e in events),
        "route_ok": message.get("provider") == PROVIDER
                    and message.get("model") == MODEL,
        "stop_ok": message.get("stopReason") == "stop",
        "text_ok": SENTINEL in text,
        "tokens_ok": isinstance(total, int) and 0 < total <= BUDGET_TOTAL_TOKENS,
    }
    checks["totalTokens"] = total
    checks["text"] = text.strip()[:120]
    return checks, ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--strict", action="store_true",
                        help="missing credentials are an infra error (exit 2) "
                             "instead of a local SKIP")
    args = parser.parse_args()

    status, detail = preflight(args.strict)
    if status == "infra":
        print(json.dumps({"verdict": "INFRA", "reason": detail}))
        return 2
    if status == "skip":
        print(json.dumps({"verdict": "SKIP", "reason": detail}))
        return 0

    checks: dict | None = None
    error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        checks, error = run_once()
        if checks is not None and checks.get("exit0") and checks.get("agent_end"):
            break
        if attempt < MAX_ATTEMPTS:
            time.sleep(BACKOFF_S)
    if checks is None:
        print(json.dumps({"verdict": "INFRA", "reason": error}))
        return 2
    failed = sorted(k for k in ("exit0", "agent_end", "route_ok", "stop_ok",
                                "text_ok", "tokens_ok") if not checks.get(k))
    if failed:
        print(json.dumps({"verdict": "FAIL", "failed": failed, **checks}))
        return 1
    print(json.dumps({"verdict": "PASS", "provider": PROVIDER, "model": MODEL,
                      "totalTokens": checks["totalTokens"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
