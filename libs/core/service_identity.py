"""Canonical service identity (alias to ``common.service_identity``, #1270)."""

from __future__ import annotations

import sys

import common.service_identity as _common_service_identity

sys.modules[__name__] = _common_service_identity
