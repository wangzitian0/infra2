"""Detect drift-prone platform image references.

infra2 deploys "from a git branch + hash gate", so a floating image tag drifts
silently upstream (the prefect `:3-latest` lockup, #253/#255). Bare `:latest` is
the clearest offender: pin a digest (or at least a specific version) so the
deployed image is reproducible. These are pure helpers used by the CI lint.
"""

from __future__ import annotations

import re

_IMAGE_RE = re.compile(r"^\s*image:\s*(\S+)\s*$")


def _tag_of(ref: str) -> str:
    """The tag of an image ref, or '' if untagged. Ignores a registry:port colon."""
    last = ref.rsplit("/", 1)[-1]
    return last.rsplit(":", 1)[-1] if ":" in last else ""


def bare_latest_violations(compose_text: str) -> list[str]:
    """Image refs that use a bare ``:latest`` tag with no digest.

    Allowed (skipped): digest-pinned refs (``...@sha256:...``) and
    variable-templated refs (``${IMAGE_TAG}`` etc. — app images carry their own
    promote-not-rebuild discipline).
    """
    violations: list[str] = []
    for line in compose_text.splitlines():
        match = _IMAGE_RE.match(line)
        if not match:
            continue
        ref = match.group(1)
        if "${" in ref or "@sha256:" in ref:
            continue
        if _tag_of(ref) == "latest":
            violations.append(ref)
    return violations
