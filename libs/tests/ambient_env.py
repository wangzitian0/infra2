"""Clear the telemetry variables of the developer shell before a test loads an app.

`platform/30.todo/app.py` and `platform/12.alerting/app.py` call `init_service` when the module
loads. `init_service` reads the service name, the version and the `OTEL_*` variables of the SDK
contract. In its default mode it also reads the tier variables and checks each alias pair.
The todo app uses the identity mode, which skips the tier variables and still checks
`SERVICE_VERSION` with `IMAGE_TAG`. Two names of one pair with different values stop a strict
start with `EnvironmentConflictError` (#1238). A shell that exports an endpoint but no
`OTEL_SERVICE_NAME` made the todo app raise while a test only loaded it.

The helper removes every variable and alias that the SDK contract declares, and every name that
starts with `OTEL_`. It reads the contract from the SDK, so it keeps no list of its own and
follows each SDK release. A test that needs a variable sets it after this call.
"""

from __future__ import annotations

import os

import pytest
from infra2_sdk.runtime.environment import runtime_env_contract


def _contract_names() -> set[str]:
    """Every variable and every alias that the SDK contract declares."""
    names: set[str] = set()
    for variable in runtime_env_contract()["variables"]:
        names.add(variable["name"])
        names.update(variable["aliases"])
    return names


def clear_telemetry_env(monkeypatch: pytest.MonkeyPatch) -> None:
    declared = _contract_names()
    for key in list(os.environ):
        if key in declared or key.startswith("OTEL_"):
            monkeypatch.delenv(key, raising=False)
