"""Frozen shim: the implementation lives in ``libs.core.constants`` (#955).

Import from ``libs.core.constants`` in new code. This module only re-exports it.
"""

from __future__ import annotations

from libs.core.constants import (
    GITHUB_BRANCH,
    GITHUB_OWNER,
    GITHUB_REPO,
)

__all__ = [
    "GITHUB_BRANCH",
    "GITHUB_OWNER",
    "GITHUB_REPO",
]
