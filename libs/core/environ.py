"""Infra2 core environment and configuration (alias to ``common.environ``, #1270)."""

from __future__ import annotations

import sys

import common.environ as _common_environ

sys.modules[__name__] = _common_environ
