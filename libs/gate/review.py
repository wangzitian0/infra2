"""Review thread scoring and automated review request logic."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from libs.gate.types import (
    AUTOMATED_REVIEWERS,
    COPILOT_BOT_ID,
    SEVERITY_WEIGHTS,
    UNLABELLED_SEVERITY_WEIGHT,
    HeadFacts,
    Runner,
)

_SEVERITY_RE = re.compile(
    r"\bseverity\s*[:：]\s*\**(high|middle|medium|low)\b", re.IGNORECASE
)
_CRITICAL_SECURITY_RE = re.compile(
    r"\b(critical|fatal|security vulnerability|sql injection|remote code execution|credential leak)\b",
    re.IGNORECASE,
)


def thread_weight(bodies: Sequence[str]) -> float:
    """The weight of one unresolved thread.

    The highest label anywhere in the thread wins: a reply that downgrades its
    own nit does not lower a `high` raised above it, and a thread resolves as a
    whole or not at all. If unlabelled, clear critical security signals weigh 1.0.
    """
    best = 0.0
    has_critical_signal = False
    for body in bodies:
        text = body or ""
        for match in _SEVERITY_RE.finditer(text):
            best = max(best, SEVERITY_WEIGHTS[match.group(1).lower()])
        if _CRITICAL_SECURITY_RE.search(text):
            has_critical_signal = True
    if best:
        return best
    if has_critical_signal:
        return SEVERITY_WEIGHTS["high"]
    return UNLABELLED_SEVERITY_WEIGHT


def request_copilot_review(facts: HeadFacts, *, gh: Runner | None = None) -> bool:
    """Ask Copilot to review the current head (it re-reviews a fix-up push only on request).

    Return True only when a re-read shows Copilot in the pending review requests.
    For the GitHub App token the mutation returns no error and registers nothing
    (#1075). An unreadable re-read also returns False: do not claim what was not seen.
    """
    if gh is None:
        from libs.gate.client import _gh

        gh = _gh

    if not facts.node_id:
        raise RuntimeError("pull request node id unknown; cannot request a review")
    gh(
        [
            "api",
            "graphql",
            "-f",
            "query=mutation{requestReviews(input:{pullRequestId:%s,botIds:[%s],union:true})"
            "{pullRequest{number}}}"
            % (json.dumps(facts.node_id), json.dumps(COPILOT_BOT_ID)),
        ]
    )
    try:
        payload = json.loads(
            gh(
                [
                    "api",
                    "graphql",
                    "-f",
                    "query=query{node(id:%s){... on PullRequest{reviewRequests(first:100)"
                    "{nodes{requestedReviewer{__typename ... on Bot{id login}}}}}}}"
                    % json.dumps(facts.node_id),
                ]
            )
        )
        nodes = payload["data"]["node"]["reviewRequests"]["nodes"]
    except (RuntimeError, json.JSONDecodeError, KeyError, TypeError):
        return False
    for node in nodes if isinstance(nodes, list) else []:
        reviewer = node.get("requestedReviewer") if isinstance(node, dict) else None
        if isinstance(reviewer, dict) and (
            reviewer.get("id") == COPILOT_BOT_ID
            or reviewer.get("login") in AUTOMATED_REVIEWERS
        ):
            return True
    return False
