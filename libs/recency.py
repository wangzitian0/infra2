"""Frozen shim: the implementation lives in ``libs.observability.recency`` (#955).

Import from ``libs.observability.recency`` in new code. This module only re-exports it.
"""

from __future__ import annotations

from libs.observability.recency import (
    ConsecutiveObservationState,
    evaluate_consecutive_hysteresis,
    is_recently_flapping,
)

__all__ = [
    "ConsecutiveObservationState",
    "evaluate_consecutive_hysteresis",
    "is_recently_flapping",
]
