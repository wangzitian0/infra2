"""Clear the telemetry variables of the developer shell before a test loads an app.

`platform/30.todo/app.py` and `platform/12.alerting/app.py` call `init_service` when the module
loads. `init_service` reads the tier variables and every `OTEL_*` variable. A shell that exports
an endpoint but no `OTEL_SERVICE_NAME` made the todo app raise while a test only loaded it.
A shell that exports `APP_ENV` makes the SDK raise `EnvironmentConflictError` when a test sets
`ENV` to another value (#1238).
A test that needs a variable sets it after this call.
"""

from __future__ import annotations

import os

import pytest

#: The tier variables. The SDK reads `ENVIRONMENT`, and `ENV` and `APP_ENV` are its aliases.
#: `libs/tests/test_ambient_env.py` checks that the helper removes every name the SDK declares.
TIER_VARIABLES = ("ENV", "ENVIRONMENT", "APP_ENV")


def clear_telemetry_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [*os.environ, *TIER_VARIABLES]:
        if key in TIER_VARIABLES or key.startswith("OTEL_"):
            monkeypatch.delenv(key, raising=False)
