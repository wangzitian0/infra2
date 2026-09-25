"""Detect drift-prone platform image references.

infra2 deploys "from a git branch + hash gate", so a floating image tag drifts
silently upstream (the prefect `:3-latest` lockup, #253/#255). Bare `:latest` is
the clearest offender: pin a digest (or at least a specific version) so the
deployed image is reproducible. These are pure helpers used by the CI lint.
"""

from __future__ import annotations

from infra2_sdk.rules.compose import (
    find_bare_latest_violations,
    tag_of_image_ref,
)

_tag_of = tag_of_image_ref
bare_latest_violations = find_bare_latest_violations

__all__ = ["bare_latest_violations", "_tag_of"]
