"""Single service registry (alias to ``common.registry``, #1270)."""

from __future__ import annotations

import sys

import common.registry as _common_registry

sys.modules[__name__] = _common_registry
