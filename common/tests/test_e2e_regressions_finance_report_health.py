"""Offline test for K4 health check evaluation in e2e_regressions (#1794)."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HEALTH_TEST_PATH = (
    ROOT / "e2e_regressions" / "tests" / "apps" / "test_finance_report_health.py"
)


def _load_is_check_healthy(monkeypatch):
    conftest = types.ModuleType("conftest")
    conftest.TestConfig = object
    monkeypatch.setitem(sys.modules, "conftest", conftest)
    spec = importlib.util.spec_from_file_location("_fr_health_test", HEALTH_TEST_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._is_check_healthy


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        ({"status": "present", "detail": "HTTP 200", "required": True}, True),
        ({"status": "healthy"}, True),
        ({"status": "ok"}, True),
        ({"status": "absent", "detail": "Not configured", "required": True}, False),
        ({"status": "unhealthy"}, False),
        ({}, False),
        (None, False),
    ],
)
def test_is_check_healthy_handles_bool_and_k4_dicts(monkeypatch, value, expected):
    is_check_healthy = _load_is_check_healthy(monkeypatch)
    assert is_check_healthy(value) is expected
