"""Backward-compatibility shim for openpanel client configuration.

Canonical implementation moved to libs.observability.openpanel (#840).
"""

from __future__ import annotations

from libs.observability.openpanel import (
    OPENPANEL_CLIENTS,
    openpanel_env,
)

__all__ = [
    "OPENPANEL_CLIENTS",
    "openpanel_env",
]
