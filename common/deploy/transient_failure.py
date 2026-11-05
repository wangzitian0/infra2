"""Tell a transient deploy failure from a deterministic one (#1439).

The release train (``tools.reconcile_iac_inputs``) retries a failed unit at most twice in
the same run and on the same tag. It retries only a failure that this module recognises as
transient: the same command can succeed a moment later without any change by a person.

The decision order is fixed and fails closed:

1. A deterministic marker wins: an auth error, a health check after a good pull, a changed
   config hash, a config error, or a runner ``error_kind`` that names a deterministic cause.
   A second run would repeat the failure, so it is not retried, even if a transient phrase
   appears in the same message.
2. A transient marker earns a retry: DNS or network (``server misbehaving``,
   ``i/o timeout``, ``TLS handshake timeout``), a registry 5xx, a runner transport blip,
   or a runner that lost (``not_found``) or interrupted a deploy.
3. Anything else is unrecognised and not retried.

Only the failure verdict is read: the text from the last ``deploy_v2 failed:`` line. A
warning that deploy_v2 printed earlier (for example the Dokploy retry notice) is not the
reason the command failed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from common.deploy.iac_runner_client import (
    RUNNER_INTERRUPTED_MARKER,
    RUNNER_NOT_FOUND_MARKER,
)

VERDICT_PREFIX = "deploy_v2 failed:"
# With no verdict line (an uncaught error), read this many trailing characters.
_TAIL_CHARS = 4000
_EXCERPT_CHARS = 200

# (kind, pattern). Checked first; the first match decides.
DETERMINISTIC_MARKERS: tuple[tuple[str, str], ...] = (
    (
        "auth",
        r"unauthori[sz]ed|authentication required|pull access denied|access denied"
        r"|permission denied|forbidden|invalid (?:credentials|token)"
        r"|'error_kind': '(?:dokploy_auth_failed|vault_permission_denied"
        r"|vault_token_missing|onepassword_auth_failed)'",
    ),
    (
        "health_check",
        r"not in service|unhealthy|health ?check|runtime verification failed"
        r"|post-deploy verification failed|/api/health",
    ),
    (
        "config_hash",
        r"effective remote config is stale|iac_config_hash|stale clone"
        r"|identity verification failed",
    ),
    (
        "config",
        r"'error_kind': '(?:vault_secret_missing|missing_python_dependency"
        r"|missing_environment_isolation)'|missing release_tag|must be one of"
        r"|is required",
    ),
)

# (kind, pattern). The first match names the cause.
TRANSIENT_MARKERS: tuple[tuple[str, str], ...] = (
    ("runner_interrupted", re.escape(RUNNER_INTERRUPTED_MARKER)),
    ("runner_not_found", re.escape(RUNNER_NOT_FOUND_MARKER)),
    ("dns", r"server misbehaving|temporary failure in name resolution"),
    ("network_timeout", r"i/o timeout"),
    ("tls_timeout", r"tls handshake timeout"),
    (
        "registry_5xx",
        r"received unexpected http status: 5\d\d"
        r"|\b50[0234] (?:internal server error|bad gateway|service unavailable"
        r"|gateway time-?out)",
    ),
    ("transient_transport", r"'error_kind': 'transient_transport'"),
)


@dataclass(frozen=True)
class FailureClass:
    """The verdict on one failed command: retry it or not, and why."""

    transient: bool
    kind: str
    excerpt: str

    @property
    def cause(self) -> str:
        """One line for the attempt record, the log and the run summary."""
        return f"{self.kind}: {self.excerpt}" if self.excerpt else self.kind


def failure_verdict(stderr: str, stdout: str = "") -> str:
    """The part of a failed command's output that says why it failed."""
    text = stderr or ""
    index = text.rfind(VERDICT_PREFIX)
    if index >= 0:
        return text[index:]
    return (text or stdout or "")[-_TAIL_CHARS:]


def _excerpt(text: str, match: re.Match) -> str:
    start = text.rfind("\n", 0, match.start()) + 1
    end = text.find("\n", match.end())
    line = text[start : end if end >= 0 else len(text)].strip()
    if len(line) <= _EXCERPT_CHARS:
        return line
    # Keep the matched phrase in view when the line is longer than the limit.
    centre = match.start() - start
    lo = max(0, min(centre - _EXCERPT_CHARS // 2, len(line) - _EXCERPT_CHARS))
    return line[lo : lo + _EXCERPT_CHARS]


def classify_failure(stderr: str, stdout: str = "") -> FailureClass:
    """Classify one failed command from its output. See the module docstring."""
    verdict = failure_verdict(stderr, stdout)
    if not verdict.strip():
        return FailureClass(False, "no_diagnostic", "")
    for kind, pattern in DETERMINISTIC_MARKERS:
        match = re.search(pattern, verdict, re.IGNORECASE)
        if match:
            return FailureClass(False, kind, _excerpt(verdict, match))
    for kind, pattern in TRANSIENT_MARKERS:
        match = re.search(pattern, verdict, re.IGNORECASE)
        if match:
            return FailureClass(True, kind, _excerpt(verdict, match))
    first_line = verdict.strip().splitlines()[0].strip()
    return FailureClass(False, "unrecognized", first_line[:_EXCERPT_CHARS])


__all__ = [
    "DETERMINISTIC_MARKERS",
    "TRANSIENT_MARKERS",
    "FailureClass",
    "classify_failure",
    "failure_verdict",
]
