"""Infra2 Core Domain Constants (alias to ``common.constants``, #1270)."""

from __future__ import annotations

import sys

import common.constants as _common_constants

sys.modules[__name__] = _common_constants
