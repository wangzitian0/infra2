"""Clear the telemetry variables of the developer shell before a test loads an app.

`platform/30.todo/app.py` and `platform/12.alerting/app.py` call `init_service` when the module
loads. `init_service` reads `ENV` and every `OTEL_*` variable. A shell that exports an
endpoint but no `OTEL_SERVICE_NAME` made the todo app raise while a test only loaded it.
A test that needs a variable sets it after this call.
"""

from __future__ import annotations

import os

import pytest


def clear_telemetry_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [*os.environ, "ENV", "ENVIRONMENT"]:
        if key in ("ENV", "ENVIRONMENT") or key.startswith("OTEL_"):
            monkeypatch.delenv(key, raising=False)
