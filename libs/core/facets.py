"""Typed per-service facet declarations (alias to ``common.facets``, #1270)."""

from __future__ import annotations

import sys

import common.facets as _common_facets

sys.modules[__name__] = _common_facets
