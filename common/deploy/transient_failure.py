"""Tell a transient deploy failure from any other (#1439).

The release train retries a failed platform unit only for a fault that a second run can
cure. The set is small and fails closed: what is not recognised is not retried, and a
marker of a failure that a second run repeats (auth, a health check after a good pull, a
changed hash) wins over a transient phrase. Only the failure verdict is read: the text from
the last ``deploy_v2 failed:`` line.
"""

from __future__ import annotations

import re

VERDICT_PREFIX = "deploy_v2 failed:"
_REGISTRY = r"(?:ghcr\.io|docker\.io|registry|/v2/|\bpull(?:ing)?\b|manifest)"
_HTTP_5XX = (
    r"\b50[0234] (?:internal server error|bad gateway|service unavailable"
    r"|gateway time-?out)"
)

# (kind, pattern). The first match names the cause. A 5xx phrase counts only on a line
# that names a registry or a pull: a version probe that answers 502 is not a registry.
TRANSIENT = (
    ("runner_interrupted", r"iac_runner reported interrupted"),
    ("runner_not_found", r"iac_runner answered not_found"),
    ("dns", r"server misbehaving"),
    ("network_timeout", r"i/o timeout"),
    ("tls_timeout", r"tls handshake timeout"),
    (
        "registry_5xx",
        rf"received unexpected http status: 5\d\d|{_REGISTRY}[^\n]*{_HTTP_5XX}"
        rf"|{_HTTP_5XX}[^\n]*{_REGISTRY}",
    ),
)
DETERMINISTIC = (
    r"not in service|unhealthy|unauthori[sz]ed|access denied|permission denied"
    r"|forbidden|is stale|stale clone"
)


def classify_failure(stderr: str) -> tuple[bool, str]:
    """``(transient, cause)`` for the output of one failed command."""
    text = stderr or ""
    index = text.rfind(VERDICT_PREFIX)
    verdict = text[index:] if index >= 0 else text[-4000:]
    if not verdict.strip():
        return False, "no_diagnostic"
    veto = re.search(DETERMINISTIC, verdict, re.IGNORECASE)
    for kind, pattern in () if veto else TRANSIENT:
        match = re.search(pattern, verdict, re.IGNORECASE)
        if match:
            start = verdict.rfind("\n", 0, match.start()) + 1
            end = verdict.find("\n", match.end())
            line = verdict[start : end if end >= 0 else None]
            # a long line: keep a window around the matched phrase
            lo = max(0, min(match.start() - start - 100, len(line) - 200))
            return True, f"{kind}: {line[lo : lo + 200].strip()}"
    return False, f"not_retried: {verdict.strip().splitlines()[0][:200]}"
